from __future__ import annotations

import pytest

from kefu.case_desk.errors import ValidationError
from kefu.relay.references import parse_quoted_case_ref


def test_reference_parser_is_case_insensitive_and_requires_one_marker() -> None:
    assert parse_quoted_case_ref("请看〔kf·8h2m7qk〕的内容") == "8H2M7QK"
    with pytest.raises(ValidationError):
        parse_quoted_case_ref("没有引用事件")
    with pytest.raises(ValidationError):
        parse_quoted_case_ref("〔KF·8H2M7QK〕 和 〔KF·A1B2C3D〕")
