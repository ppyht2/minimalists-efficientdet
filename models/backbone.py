import collections
import math
import torch
from torch import nn
import torch.nn.functional as F
from typing import List


ModelParams = collections.namedtuple(
    "ModelParams",
    ["width_coefficient", "depth_coefficient", "resolution", "dropout_rate"],
)

BlockParams = collections.namedtuple(
    "BlockParams",
    [
        "repetition",
        "kernel_size",
        "stride",
        "in_channels",
        "out_channels",
        "expand_ratio",
        "squeeze_ratio",
    ],
)


def get_efficientnet_params(model_name):
    params_dict = {
        "efficientnet-b0": ModelParams(1.0, 1.0, 224, 0.2),
        "efficientnet-b1": ModelParams(1.0, 1.1, 240, 0.2),
        "efficientnet-b2": ModelParams(1.1, 1.2, 260, 0.3),
        "efficientnet-b3": ModelParams(1.2, 1.4, 300, 0.3),
        "efficientnet-b4": ModelParams(1.4, 1.8, 380, 0.4),
        "efficientnet-b5": ModelParams(1.6, 2.2, 456, 0.4),
        "efficientnet-b6": ModelParams(1.8, 2.6, 528, 0.5),
        "efficientnet-b7": ModelParams(2.0, 3.1, 600, 0.5),
        "efficientnet-b8": ModelParams(2.2, 3.6, 672, 0.5),
        "efficientnet-l2": ModelParams(4.3, 5.3, 800, 0.5),
    }
    return params_dict[model_name]


def get_default_blocks_params() -> List[BlockParams]:
    return [
        BlockParams(1, 3, 1, 32, 16, 1, 0.25),
        BlockParams(2, 3, 2, 16, 24, 6, 0.25),
        BlockParams(2, 5, 2, 24, 40, 6, 0.25),
        BlockParams(3, 3, 2, 40, 80, 6, 0.25),
        BlockParams(3, 5, 1, 80, 112, 6, 0.25),
        BlockParams(4, 5, 2, 112, 192, 6, 0.25),
        BlockParams(1, 3, 1, 192, 320, 6, 0.25),
    ]


class Swish(nn.Module):
    def forward(self, x):
        return x * torch.sigmoid(x)


def scale_repeats(depth: int, model_params: ModelParams):
    """ compound scaling for depth coefficinet """
    return math.ceil(depth * model_params.depth_coefficient)


def scale_filters(filter: int, model_params: ModelParams):
    """ compound scaling for width coefficient """
    divisor: int = 8  # ensure filter size is divisible by 8
    estimated_filter = filter * model_params.width_coefficient

    # round to the nearest multipler of divisor
    multiplier = round((estimated_filter / divisor))
    actual_filter = int(multiplier) * divisor
    return actual_filter


def compound_scaling(
    block_params: BlockParams, model_params: ModelParams
) -> BlockParams:
    return block_params._replace(
        in_channels=scale_filters(block_params.in_channels, model_params),
        out_channels=scale_filters(block_params.out_channels, model_params),
        repetition=scale_repeats(block_params.repetition, model_params),
    )


class StemBlock(nn.Module):
    IN_CHANNELS: int = 3
    STRIDE: int = 2
    KERNEL_SIZE: int = 3
    OUT_CHANNELS: int = 32
    PADDING: int = 1

    def __init__(self, model_params: ModelParams):
        super(StemBlock, self).__init__()
        self._model_params = model_params

        self._out_channels = scale_filters(
            StemBlock.OUT_CHANNELS, self._model_params
        )

        self._conv = nn.Conv2d(
            in_channels=StemBlock.IN_CHANNELS,
            out_channels=self.out_channels,
            kernel_size=StemBlock.KERNEL_SIZE,
            stride=StemBlock.STRIDE,
            padding=StemBlock.PADDING,
            bias=False,
        )

        self._bn = nn.BatchNorm2d(num_features=self.out_channels)

        self._act = Swish()

    def forward(self, x):
        x = self._conv(x)
        x = self._bn(x)
        x = self._act(x)
        return x


class MBConvBlock(nn.Module):
    """ this module has several components

    # 1. expansion from residual bottle neck
    # 2. depthwise convolution
    # 3. squeeze and excitation
    # 4. projection
    """

    def __init__(self, block_params: BlockParams):
        self._block_params = block_params

        self._act = Swish()

        # 1. Expansion
        exp_in_channels = self._block_params.in_channels
        exp_out_channels = exp_in_channels * self._block_params.expand_ratio
        self._expand_conv = nn.Conv2d(
            in_channels=exp_in_channels,
            out_channels=exp_out_channels,
            kernel_size=1,
            bias=False,
            # TODO: shall we consider padding here?
        )
        self._expand_bn = nn.BatchNorm2d(num_features=exp_out_channels)

        # 2. Depthwise Convolutions
        self._depthwise_conv = nn.Conv2d(
            in_channels=exp_out_channels,
            out_channels=exp_out_channels,
            groups=exp_out_channels,  # depthwise
            kernel_size=self._block_params.kernel_size,
            stride=self._block_params.stride,
            bias=False,
        )
        self._depthwise_bn = nn.BatchNorm2d(num_features=exp_out_channels)

        # 3. Squeeze and Excitation
        squeezed_channels = max(
            1,
            int(
                self._block_params.in_channels
                * self._block_params.squeeze_ratio
            ),
        )

        self._squeeze_conv = nn.Conv2d(
            in_channels=exp_out_channels,
            out_channels=squeezed_channels,
            kernel_size=1,
        )

        self._excitation_conv = nn.Conv2d(
            in_channels=squeezed_channels,
            out_channels=exp_out_channels,
            kernel_size=1,
        )

        # 4. Projection
        prj_out_channels = self._block_params.out_channels
        self._project_conv = nn.Conv2d(
            in_channels=exp_out_channels,
            out_channels=prj_out_channels,
            kernel_size=1,
            bias=False,
        )
        self._project_bn = nn.BatchNorm2d(num_features=prj_out_channels)

    def forward(self, xi):
        x = xi

        # 1. Expansion
        x = self._expand_conv(x)
        x = self._expand_bn(x)
        x = self._act(x)

        # 2. Depthwise Convolution
        x = self._depthwise_conv(x)
        x = self._depthwise_bn(x)
        x = self._act(x)

        # 3. Squeeze and Excitation
        x_se = x

        # squeeze
        x_se = F.adaptive_avg_pool2d(x_se, 1)  # global average pool
        x_se = self._squeeze_conv(x_se)
        x_se = self._act(x_se)

        # excitation
        x_se = self._excitation_conv(x_se)
        x_se = torch.sigmoid(x_se)

        x = x_se * x

        # 4. Projection
        x = self._project_conv(x)
        x = self._project_bn(x)

        # skip connection if possible
        if (
            self._block_params.stride == 1
            and self._block_params.in_channels
            == self._block_params.out_channels
        ):
            x = x + xi

        return x


class EfficientNet(nn.Module):
    def __init__(
        self, model_params: ModelParams,
    ):
        self._model_params = model_params
        self._blocks = nn.ModuleList([])

        # Stem
        self._blocks.append(StemBlock(self._model_params))

        # TODO: there a better abstraction?
        # compound scaling
        for block_params in get_default_blocks_params():
            scaled_params = compound_scaling(block_params, self._model_params)

            self._blocks.append(MBConvBlock(scaled_params))
            # enbale skip connections on repeats
            if scaled_params.repetition > 1:
                scaled_params = scaled_params._replace(
                    in_channels=scaled_params.out_channels, stride=1
                )

            for _ in range(scaled_params.repetition - 1):
                self._blocks.append(MBConvBlock(scaled_params))

        # classification head

    def forward(self, x):
        pass
