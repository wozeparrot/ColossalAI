from functools import partial

import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp

import colossalai
from colossalai.gemini import TensorState
from colossalai.gemini.chunk import Chunk
from colossalai.tensor import ColoParameter
from colossalai.tensor import ProcessGroup as ColoProcessGroup
from colossalai.testing import parameterize, rerun_if_address_is_in_use
from colossalai.utils import free_port, get_current_device


def dist_sum(x):
    temp = torch.tensor([x], device=get_current_device())
    dist.all_reduce(temp)
    return temp.item()


def add_param(param_list, param_cp_list, *args, **kwargs):
    param = ColoParameter(torch.randn(*args, **kwargs))
    param_list.append(param)
    param_cp_list.append(param.clone())


def check_euqal(param, param_cp):
    if param.device != param_cp.device:
        temp = param.data.to(param_cp.device)
    else:
        temp = param.data
    return torch.equal(temp, param_cp.data)


@parameterize('init_device', [None, torch.device('cpu')])
@parameterize('keep_gathered', [True, False])
@parameterize('pin_memory', [True, False])
def exam_chunk_basic(init_device, keep_gathered, pin_memory):
    world_size = torch.distributed.get_world_size()
    pg = ColoProcessGroup()
    my_chunk = Chunk(chunk_size=1024,
                     process_group=pg,
                     dtype=torch.float32,
                     init_device=init_device,
                     cpu_shard_init=True,
                     keep_gathered=keep_gathered,
                     pin_memory=pin_memory)

    param_list = []
    param_cp_list = []

    add_param(param_list, param_cp_list, 8, 8, 8, device='cuda')
    add_param(param_list, param_cp_list, 4, 4)
    add_param(param_list, param_cp_list, 4, 8, 2, device='cuda')
    add_param(param_list, param_cp_list, 1, 1, 5)

    for param in param_list:
        my_chunk.append_tensor(param)
    assert my_chunk.utilized_size == 597
    for param, param_cp in zip(param_list, param_cp_list):
        check_euqal(param, param_cp)
    my_chunk.close_chunk()

    if keep_gathered is False:
        assert my_chunk.cpu_shard.size(0) == 1024 // world_size
        assert my_chunk.device_type == 'cpu'
        assert my_chunk.can_move
        my_chunk.shard_move(get_current_device())
    else:
        assert my_chunk.chunk_total.size(0) == 1024
        assert my_chunk.device_type == 'cuda'
        assert not my_chunk.can_move

    assert dist_sum(my_chunk.valid_end) == my_chunk.utilized_size
    flag = my_chunk.has_inf_or_nan
    assert not flag, "has_inf_or_nan is {}".format(flag)

    my_chunk.access_chunk()
    assert my_chunk.device_type == 'cuda'
    for param, param_cp in zip(param_list, param_cp_list):
        check_euqal(param, param_cp)

    assert my_chunk.tensors_state_monitor[TensorState.HOLD] == 4
    my_chunk.tensor_trans_state(param_list[0], TensorState.COMPUTE)
    assert my_chunk.tensors_state_monitor[TensorState.HOLD] == 3
    assert my_chunk.tensors_state_monitor[TensorState.COMPUTE] == 1
    assert not my_chunk.can_release

    for param in param_list:
        my_chunk.tensor_trans_state(param, TensorState.COMPUTE)
        my_chunk.tensor_trans_state(param, TensorState.READY_FOR_REDUCE)

    assert my_chunk.tensors_state_monitor[TensorState.READY_FOR_REDUCE] == 4
    assert my_chunk.can_reduce
    my_chunk.reduce()
    assert my_chunk.tensors_state_monitor[TensorState.HOLD] == 4

    if keep_gathered is False:
        assert my_chunk.cuda_shard.size(0) == 1024 // world_size
        assert my_chunk.device_type == 'cuda'
        assert my_chunk.can_move
    else:
        assert my_chunk.chunk_total.size(0) == 1024
        assert my_chunk.device_type == 'cuda'
        assert not my_chunk.can_move


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    exam_chunk_basic()


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [1, 2, 4])
@rerun_if_address_is_in_use()
def test_chunk_function(world_size):
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_chunk_function(4)
