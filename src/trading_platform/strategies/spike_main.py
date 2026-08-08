"""兼容旧进程入口；实现已归档到 :mod:`strategies.spike.main`。"""

import sys

from trading_platform.strategies.spike import main as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
