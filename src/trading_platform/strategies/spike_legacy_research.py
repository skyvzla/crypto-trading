"""兼容旧导入路径；实现已归档到 :mod:`strategies.spike.legacy_research`。"""

import sys

from trading_platform.strategies.spike import legacy_research as _implementation

sys.modules[__name__] = _implementation
