# -*- coding: utf-8 -*-
"""pytest 全局配置：保证项目根在 sys.path，提供通用 fixture。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
