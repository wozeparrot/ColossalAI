import functools
from time import time
from typing import List, Optional, Tuple

import torch

from colossalai.gemini.chunk import Chunk, ChunkManager

from .memory_tracer.memstats_collector import MemStatsCollectorV2, MemStatsCollectorStatic
from .placement_policy import PlacementPolicyFactory


class GeminiManager:
    """
    Stateful Tensor Manager, inspired from PatrickStar

    PatrickStar: Parallel Training of Pre-trained Models via Chunk-based Memory Management
    https://arxiv.org/abs/2108.05818

    Args:
        placement_policy (str): Which device to place *held* tensors. It can be 'cpu', 'cuda' and 'auto'.
            If it's 'cpu', parameters, gradients and optimizer states will be offloaded to CPU, which means min CUDA memory will be used.
            If it's 'cuda', they won't be offloaded, which means max CUDA memory will be used.
            If it's 'auto', they are moving dynamically based on CPU and CUDA memory usage. It will utilize heterogeneous memory space evenly and well.
            Note that 'auto' policy can only work well when no other processes use CUDA during your training.
        chunk_manager (ChunkManager): A ``ChunkManager`` instance.
    """

    def __init__(self, placement_policy: str,
                 chunk_manager: ChunkManager,
                 module: Optional[torch.nn.Module] = None,
                 use_static_memstats: bool = False) -> None:

        assert placement_policy in PlacementPolicyFactory.get_polocy_names()
        self.policy_name = placement_policy
        policy_cls = PlacementPolicyFactory.create(placement_policy)
        self._chunk_manager = chunk_manager
        # self._mem_stats_collector = MemStatsCollectorV2(chunk_manager) if policy_cls.need_mem_stats else None
        self.use_static_memstats = use_static_memstats
        if policy_cls.need_mem_stats:
            if use_static_memstats:
                assert module is not None
                self._mem_stats_collector = MemStatsCollectorStatic(module, chunk_manager)
            else:
                self._mem_stats_collector = MemStatsCollectorV2(chunk_manager)
        else:
            self._mem_stats_collector = None

        self._placement_policy = policy_cls(chunk_manager, self._mem_stats_collector)
        self._compute_list: List[Tuple[Chunk, ...]] = []
        self._compute_idx: int = -1

        self._h2d_volume = 0
        self._d2h_volume = 0
        self._layout_time = 0
        self._evict_time = 0
        self._warmup = True
        self._comp_cuda_demand_time = 0

    def pre_iter(self, *args):
        if self._mem_stats_collector and self._warmup:
            if self.use_static_memstats:
                self._mem_stats_collector.init_mem_stats(*args)
                self._warmup = False
            else:
                self._mem_stats_collector.start_collection()

    def post_iter(self):
        """This function must be called when each iteration finishes
        """
        if self._mem_stats_collector and self._warmup:
            self._mem_stats_collector.finish_collection()
        self._warmup = False
        self._compute_idx = -1
        self._h2d_volume = 0
        self._d2h_volume = 0
        self._layout_time = 0
        self._evict_time = 0
        self._comp_cuda_demand_time = 0

    def adjust_layout(self, chunks: Tuple[Chunk, ...]) -> None:
        """ Adjust the layout of stateful tensors according to the information provided
        by mem_stats_collector, which should belongs to a Sharded Model.
        """
        # find stateful tensor in state COMPUTE
        start = time()
        self._record_chunks_order(chunks)
        cuda_demand, hold_cuda_tensor_list = self._get_layout_info(self._compute_idx, self._warmup, chunks)
        self._layout_time += time() - start

        vol, evict_time = self._placement_policy.evict_tensors(can_evict_chunks=hold_cuda_tensor_list,
                                                               cuda_demand=cuda_demand,
                                                               warmup=self._warmup,
                                                               compute_list=self._compute_list,
                                                               compute_idx=self._compute_idx)

        self._d2h_volume += vol
        self._evict_time += evict_time
        # move COMPUTE tensors to CUDA
        self._h2d_volume += cuda_demand

    @functools.lru_cache(maxsize=None)
    def _get_layout_info(self, compute_idx: int, warmup: bool, chunks: Tuple[Chunk, ...]):
        start = time()
        cuda_demand = 0
        for chunk in chunks:
            if chunk.device_type == 'cuda':
                if chunk.is_gathered:
                    pass
                else:
                    cuda_demand += chunk.chunk_mem - chunk.shard_mem
            elif chunk.device_type == 'cpu':
                cuda_demand += chunk.chunk_mem
            else:
                raise RuntimeError
        self._comp_cuda_demand_time += time() - start

        can_evict_chunks = self._chunk_manager.get_cuda_movable_chunks()
        return cuda_demand, can_evict_chunks

    def _record_chunks_order(self, chunks: Tuple[Chunk, ...]) -> None:
        self._compute_idx += 1
        if self._warmup and self._placement_policy.need_mem_stats:
            self._compute_list.append(chunks)

    @property
    def default_device(self):
        return self._placement_policy.get_default_device()

    def sample_overall_data(self):
        if self._mem_stats_collector:
            self._mem_stats_collector.sample_overall_data()

    def sample_model_data(self):
        if self._mem_stats_collector:
            self._mem_stats_collector.sample_model_data()

    @property
    def chunk_manager(self):
        return self._chunk_manager

    @property
    def cuda_margin_mem(self) -> Optional[float]:
        if self._mem_stats_collector:
            return self._mem_stats_collector.cuda_margin_mem
        return None

    @property
    def is_cuda_margin_mem_avail(self) -> bool:
        return self._placement_policy.need_mem_stats

    @staticmethod
    def get_default_device(policy_name: str) -> torch.device:
        return PlacementPolicyFactory.get_default_device(policy_name)
