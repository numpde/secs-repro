"""Project supported formula typography without interpreting chemical syntax."""

import re
from unicodedata import category


_SUBSCRIPT_DIGITS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')
_TERM_SPACES = re.compile(r'(?<=[A-Za-z]) +(?=[0-9])|(?<=[A-Za-z0-9]) +(?=[A-Z])')


def is_formula_space(char: str) -> bool:
    """Accept Unicode space separators, excluding tabs, line breaks and controls."""
    return category(char) == 'Zs'


def normalize_formula_notation(text: str) -> str:
    """Remove presentation differences; leave split symbols/counts and controls invalid.

    Unicode space separators may separate an element from its count or the next
    element. This deliberately leaves chemistry validation to the SECS parser.
    """
    text = ''.join(' ' if is_formula_space(char) else char
                   for char in text.translate(_SUBSCRIPT_DIGITS))
    return _TERM_SPACES.sub('', text.strip(' '))
