"""Local query expansion for Japanese metadata retrieval."""

from __future__ import annotations


_SYNONYMS: dict[str, tuple[str, ...]] = {
    "员工": ("社員", "職員", "社員番号", "職員番号"),
    "职员": ("社員", "職員", "社員番号", "職員番号"),
    "社員": ("员工", "職員", "社員番号"),
    "職員": ("员工", "社員", "職員番号"),
    "姓名": ("氏名", "漢字氏名", "カナ氏名", "CNAMEKNJ", "CNAMEKNA"),
    "名字": ("氏名", "漢字氏名", "カナ氏名", "CNAMEKNJ"),
    "姓": ("氏名", "漢字氏名", "カナ氏名", "CNAMEKNJ"),
    "氏名": ("姓名", "漢字氏名", "カナ氏名"),
    "基本情况": ("基本情報", "基本資料", "給与基本情報"),
    "基本信息": ("基本情報", "基本資料", "給与基本情報"),
    "基本資料": ("基本情報", "給与基本情報"),
    "基本情報": ("基本信息", "基本資料", "給与基本情報"),
    "异动": ("異動", "任免", "発令"),
    "異動": ("异动", "任免", "発令"),
}


def expand_query_for_metadata(text: str) -> str:
    """Append stable Japanese/Chinese synonyms without an LLM round trip."""
    value = str(text or "").strip()
    extras: list[str] = []
    for key, synonyms in _SYNONYMS.items():
        if key in value:
            extras.extend(synonyms)
    deduped = list(dict.fromkeys(part for part in extras if part and part not in value))
    return " ".join([value, *deduped]).strip()
