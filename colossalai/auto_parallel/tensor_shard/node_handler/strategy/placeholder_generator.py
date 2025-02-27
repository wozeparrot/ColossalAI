from typing import List

from colossalai.auto_parallel.tensor_shard.sharding_strategy import (MemoryCost, ShardingStrategy, TrainCycleItem)

from .strategy_generator import StrategyGenerator

__all__ = ['PlaceholderGenerator']


class PlaceholderGenerator(StrategyGenerator):
    """
    PlaceholderGenerator is a generic class to generate strategies for placeholder node.
    """

    def validate(self) -> bool:
        return super().validate()

    def update_compute_cost(self, strategy: ShardingStrategy):
        compute_cost = TrainCycleItem(fwd=10, bwd=10, total=20)
        strategy.compute_cost = compute_cost

    def update_memory_cost(self, strategy: ShardingStrategy):
        '''
        Compute the memory cost per device with this specific strategy.
        '''
        forward_size_mapping = {'output': self._compute_size_in_bytes(strategy, "output")}

        # compute fwd cost incurred
        # fwd_cost = output
        fwd_activation_cost = sum([v for k, v in forward_size_mapping.items()])
        fwd_mem_cost = MemoryCost(activation=fwd_activation_cost, parameter=0)

        bwd_mem_cost = MemoryCost(activation=0, parameter=0)

        # compute total cost
        total_mem_cost = MemoryCost(activation=fwd_activation_cost, parameter=0)
        memory_cost = TrainCycleItem(fwd=fwd_mem_cost, bwd=bwd_mem_cost, total=total_mem_cost)
        strategy.memory_cost = memory_cost

    def collate_strategies(self) -> List[ShardingStrategy]:
        dim_partition_dict_mapping = {
            "output": {},
        }
        communication_action_mapping = {}
        sharding_spec_mapping = self.to_sharding_spec_mapping(dim_partition_dict_mapping)

        name = 'Replica Placeholder'

        strategy = self.get_sharding_strategy(name=name,
                                              sharding_spec_mapping=sharding_spec_mapping,
                                              communication_action_mapping=communication_action_mapping)

        return [strategy]
