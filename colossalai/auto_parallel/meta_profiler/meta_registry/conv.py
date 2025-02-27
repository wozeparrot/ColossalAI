from typing import Callable, Dict, List, Tuple, Union

import torch

from colossalai.auto_parallel.tensor_shard.sharding_strategy import (
    MemoryCost,
    OperationData,
    OperationDataType,
    ShardingStrategy,
    StrategiesVector,
    TrainCycleItem,
)
from colossalai.fx.profiler.memory_utils import activation_size
from colossalai.fx.profiler.opcount import flop_mapping
from colossalai.tensor.sharding_spec import ShardingSpec

from ..registry import meta_register

__all__ = ['convnd_meta_info']


@meta_register.register(torch.nn.Conv1d)
@meta_register.register(torch.nn.Conv2d)
@meta_register.register(torch.nn.Conv3d)
def convnd_meta_info(*args) -> Tuple[TrainCycleItem, TrainCycleItem, List[torch.Tensor]]:
    """torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d meta info generator
    The atens graph of torch.nn.Convnd with bias is
    graph():
    %input_2 : [#users=2] = placeholder[target=placeholder](default=)
    %convolution_default : [#users=1] = call_function[target=torch.ops.aten.convolution.default](args = (%input_2, None, None, [None, None, None], [None, None, None], [None, None, None], None, [None, None, None], None), kwargs = {})
    %zeros_like_default : [#users=1] = call_function[target=torch.ops.aten.zeros_like.default](args = (%convolution_default,), kwargs = {dtype: None, layout: None, device: None, pin_memory: None})
    %detach_default : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%input_2,), kwargs = {})
    %convolution_backward_default : [#users=3] = call_function[target=torch.ops.aten.convolution_backward.default](args = (%zeros_like_default, %detach_default, None, [None], [None, None, None], [None, None, None], [None, None, None], None, [None, None, None], None, [None, None, None]), kwargs = {})
    %detach_default_1 : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%convolution_backward_default,), kwargs = {})
    %detach_default_2 : [#users=0] = call_function[target=torch.ops.aten.detach.default](args = (%detach_default_1,), kwargs = {})
    %detach_default_3 : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%convolution_backward_default,), kwargs = {})
    %detach_default_4 : [#users=0] = call_function[target=torch.ops.aten.detach.default](args = (%detach_default_3,), kwargs = {})
    %detach_default_5 : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%convolution_backward_default,), kwargs = {})
    %detach_default_6 : [#users=0] = call_function[target=torch.ops.aten.detach.default](args = (%detach_default_5,), kwargs = {})

    The atens graph of torch.nn.Convnd without bias is
    graph():
    %input_2 : [#users=2] = placeholder[target=placeholder](default=)
    %convolution_default : [#users=1] = call_function[target=torch.ops.aten.convolution.default](args = (%input_2, None, None, [None, None], [None, None], [None, None], None, [None, None], None), kwargs = {})
    %zeros_like_default : [#users=1] = call_function[target=torch.ops.aten.zeros_like.default](args = (%convolution_default,), kwargs = {dtype: None, layout: None, device: None, pin_memory: None})
    %detach_default : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%input_2,), kwargs = {})
    %convolution_backward_default : [#users=2] = call_function[target=torch.ops.aten.convolution_backward.default](args = (%zeros_like_default, %detach_default, None, [None], [None, None], [None, None], [None, None], None, [None, None], None, [None, None, None]), kwargs = {})
    %detach_default_1 : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%convolution_backward_default,), kwargs = {})
    %detach_default_2 : [#users=0] = call_function[target=torch.ops.aten.detach.default](args = (%detach_default_1,), kwargs = {})
    %detach_default_3 : [#users=1] = call_function[target=torch.ops.aten.detach.default](args = (%convolution_backward_default,), kwargs = {})
    %detach_default_4 : [#users=0] = call_function[target=torch.ops.aten.detach.default](args = (%detach_default_3,), kwargs = {})

    Returns:
        Tuple[TrainCycleItem, TrainCycleItem, List[torch.Tensor]]: compute cost, memory cost and forward inputs
    """

    has_bias: bool = False
    input_tensor = next(filter(lambda x: x.type == OperationDataType.ARG, args)).data
    output_tensor = next(filter(lambda x: x.type == OperationDataType.OUTPUT, args)).data
    weight_tensor = next(filter(lambda x: x.name == 'weight', args)).data

    # check if conv has bias
    if len(args) == 4:
        bias_tensor = next(filter(lambda x: x.name == 'bias', args)).data
        has_bias = True

    # construct input args for forward
    fwd_args = [None] * 9

    # weight and input
    fwd_args[0] = input_tensor
    fwd_args[1] = weight_tensor
    fwd_args[2] = bias_tensor if has_bias else None

    # transpose indicator should be set to False
    fwd_args[6] = False

    # construct input args for backward
    bwd_args = [None] * 11

    # weight and input
    bwd_args[0] = output_tensor
    bwd_args[1] = input_tensor
    bwd_args[2] = weight_tensor
    bwd_args[-1] = [True, True, True] if has_bias else [True, True, False]

    # calculate cost
    # the fwd op with compute cost is convolution.default
    # the bwd op with compute cost is convolution_backward.default

    # calculate compute cost
    fwd_compute_cost = flop_mapping[torch.ops.aten.convolution.default](fwd_args, (output_tensor,))
    bwd_compute_cost = flop_mapping[torch.ops.aten.convolution_backward.default](bwd_args, (input_tensor, weight_tensor, bias_tensor)) if has_bias else \
                       flop_mapping[torch.ops.aten.convolution_backward.default](bwd_args, (input_tensor, weight_tensor))
    compute_cost = TrainCycleItem(fwd=fwd_compute_cost, bwd=bwd_compute_cost, total=fwd_compute_cost + bwd_compute_cost)

    # calculate memory cost
    # TODO: use profiler to check conv temp memory
    fwd_memory_cost = MemoryCost(activation=activation_size(output_tensor),
                                 parameter=activation_size(weight_tensor) +
                                 activation_size(bias_tensor) if has_bias else activation_size(weight_tensor),
                                 temp=0,
                                 buffer=0)

    bwd_memory_cost = MemoryCost(activation=activation_size(input_tensor) + activation_size(weight_tensor) +
                                 activation_size(bias_tensor) if has_bias else activation_size(input_tensor) +
                                 activation_size(weight_tensor),
                                 parameter=activation_size(weight_tensor) +
                                 activation_size(bias_tensor) if has_bias else activation_size(weight_tensor),
                                 temp=0,
                                 buffer=0)

    # total cost is the sum of forward and backward cost
    total_cost = MemoryCost(activation=fwd_memory_cost.activation + bwd_memory_cost.activation,
                            parameter=fwd_memory_cost.parameter + bwd_memory_cost.parameter)

    memory_cost = TrainCycleItem(fwd=fwd_memory_cost, bwd=bwd_memory_cost, total=total_cost)

    # store fwd_in
    fwd_in = [input_tensor]

    return compute_cost, memory_cost, fwd_in
