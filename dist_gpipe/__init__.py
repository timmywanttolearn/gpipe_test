"""
Author: your name
Date: 2022-03-07 19:36:33
LastEditTime: 2022-03-16 00:57:44
LastEditors: your name
Description: 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
FilePath: /research/gpipe_test/dist_gpipe/__init__.py
"""
from dist_gpipe.dist_gpipe import dist_gpipe
from .distributedlayers import Reshape1, Reshape2
from .compression.compression_layer import (
    RemoteQuantizationLayer,
    RemoteDeQuantizationLayer,
)
