"""Local Traditional Chinese conversion for outgoing copies, never source data."""
from functools import lru_cache
import re


_LINK = re.compile(r"(?:https?://|tg://|www\.)[^\s<>]+", re.IGNORECASE)


@lru_cache(maxsize=1)
def _converter():
    from opencc import OpenCC
    return OpenCC("s2t")


def traditional_text(value):
    """Preserve source URLs verbatim; names, figures and English stay intact."""
    text = str(value or "")
    converter = _converter()
    output, previous = [], 0
    for match in _LINK.finditer(text):
        output.extend((converter.convert(text[previous:match.start()]), match.group()))
        previous = match.end()
    output.append(converter.convert(text[previous:]))
    return "".join(output)


def prepare_payload(payload):
    """Convert only display fields. This does not call a model or translate English."""
    result = dict(payload)
    for field in ("title", "text", "summary"):
        result[field] = traditional_text(payload.get(field))
    # Convert link labels and surrounding text only; hrefs stay verbatim.
    if isinstance(payload.get("segments"), list):
        result["segments"] = [[traditional_text(item[0]), item[1]] for item in payload["segments"]
                              if isinstance(item, (list, tuple)) and len(item) == 2]
    return result
