"""Arabic as the checks read it: which characters are Arabic, and the letters folded so
that one word written two ways compares the same.

The folding is CAMeL Tools' (New York University Abu Dhabi, MIT licence, camel_tools 1.6.0
``utils/charsets.py`` and ``utils/normalize.py``): ``dediac_ar`` drops the diacritics, and
``normalize_alef_ar``, ``normalize_alef_maksura_ar`` and ``normalize_teh_marbuta_ar`` write
each letter one way. Those four are character maps, kept here as the library writes them:
the package itself depends on torch and transformers (several gigabytes on Linux) for
models this reader never loads. The tatweel, a stretch mark with no sound ("محـمد"), is
dropped as well; the library leaves it.
"""

from __future__ import annotations

import re

#: CAMeL Tools' AR_DIAC_CHARSET: fathatan to sukun, and the superscript alef
_DIAC = str.maketrans("", "", "\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670")
#: normalize_alef_ar (hamza below, hamza above, wasla, madda to a plain alef),
#: normalize_alef_maksura_ar (to yeh) and normalize_teh_marbuta_ar (to heh)
_LETTERS = str.maketrans({"\u0625": "\u0627", "\u0623": "\u0627", "\u0671": "\u0627", "\u0622": "\u0627",
                          "\u0649": "\u064a", "\u0629": "\u0647"})

#: the Arabic blocks: Arabic, its supplement and extended-A, and the presentation forms
#: some PDFs carry ("ﻣﺤﻤﺪ")
ARABIC = "؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"
ARABIC_RE = re.compile(f"[{ARABIC}]")

_TATWEEL = "ـ"


def fold(text: str) -> str:
    """Diacritics and tatweel dropped, alef, alef maksura and teh marbuta written one way."""
    return text.replace(_TATWEEL, "").translate(_DIAC).translate(_LETTERS)
