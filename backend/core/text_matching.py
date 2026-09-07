"""Shared text-matching helpers.

Lives apart from intent_recognizer so that skill_loader can reuse it without
pulling in the Anthropic SDK.
"""
import re


def keyword_matches(keyword: str, message: str) -> bool:
    """Word-boundary keyword match.

    English needs boundaries that Chinese did not: a bare substring test makes
    'return' fire on 'returning' and 'hi' fire on 'shipping'. Keywords that do
    not start and end with a word character ('?', "can't log in") fall back to a
    substring test, where a word boundary would not behave.
    """
    if not keyword:
        return False
    if keyword[0].isalnum() and keyword[-1].isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", message) is not None
    return keyword in message
