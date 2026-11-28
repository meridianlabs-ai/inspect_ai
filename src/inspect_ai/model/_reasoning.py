import base64
import html
import json
import re
from array import array
from bisect import bisect_left
from logging import getLogger
from typing import Iterator, Literal, NamedTuple, Sequence

from pydantic import JsonValue

from inspect_ai._util.content import (
    Content,
    ContentReasoning,
    ContentText,
)
from inspect_ai.model._chat_message import ChatMessage, ChatMessageAssistant
from inspect_ai.model._internal import NotInternal, decode_internal

logger = getLogger(__name__)


# Fixed effort -> token budget table used to bridge `reasoning_effort` onto
# providers that only accept an explicit token budget (Anthropic Claude 3.7-4.5,
# Google Gemini 2.5). Magnitudes mirror the existing Anthropic max_tokens-sizing
# table in anthropic.py (with `minimal` added) and clear Anthropic's 1024-token
# API floor across the board.
_EFFORT_TO_TOKENS: dict[str, int] = {
    "minimal": 2048,
    "low": 4096,
    "medium": 10000,
    "high": 16000,
    "xhigh": 32000,
    "max": 32000,
}


def effort_to_reasoning_tokens(
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    | str
    | None,
) -> int | None:
    """Translate a `reasoning_effort` value into a token budget.

    Returns None for `None` and `"none"` (no reasoning requested). Returns the
    mapped int for any supported effort level.
    """
    if effort is None or effort == "none":
        return None
    return _EFFORT_TO_TOKENS.get(effort)


def clamp_reasoning_effort_to_low_medium_high(
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    | str
    | None,
) -> Literal["low", "medium", "high"] | None:
    """Clamp a `reasoning_effort` value to the `low`/`medium`/`high` tier.

    Used by providers that pass effort through to upstream APIs which only
    accept the three-level scale. Returns None for `None` and `"none"`.
    """
    if effort is None or effort == "none":
        return None
    match effort:
        case "minimal" | "low":
            return "low"
        case "medium":
            return "medium"
        case "high" | "xhigh" | "max":
            return "high"
    return None


def clamp_reasoning_effort_to_minimal_low_medium_high(
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    | str
    | None,
) -> Literal["minimal", "low", "medium", "high"] | None:
    """Clamp a `reasoning_effort` value to the `minimal`/`low`/`medium`/`high` tier.

    Used by providers that pass effort through to upstream APIs which accept
    `minimal` in addition to the three-level scale (Perplexity); only the
    top-end values (`xhigh`/`max`) are clamped down to `high`. Returns None for
    `None` and `"none"`.
    """
    if effort is None or effort == "none":
        return None
    match effort:
        case "minimal":
            return "minimal"
        case "low":
            return "low"
        case "medium":
            return "medium"
        case "high" | "xhigh" | "max":
            return "high"
    return None


class ReasoningCapsule(NamedTuple):
    reasoning: str
    signature: str | None = None
    redacted: bool = False
    summary: str | None = None
    internal: JsonValue | None = None


def parse_content_with_reasoning(content: str) -> tuple[str, ReasoningCapsule | None]:
    """
    Looks for and extracts <think/> tags into reasoning text.

    Returns a tuple:
    - The first element is the input content with the <think> tag and its contents fully removed.
    - The second element is a ReasoningCapsule named tuple (or None if no <think> tag is found).
    """
    block = next(_think_blocks(content), None)
    if block is None:
        return content, None
    return (content[: block.start] + content[block.end :]).strip(), block.capsule


def split_content_with_reasoning(content: str) -> list[str | ReasoningCapsule]:
    """Split content into its text and every <think/> block, in order.

    This is the inverse of serializing each reasoning block of a message with
    `reasoning_to_think_tag` between its text (as agent bridges do for the
    chat completions API). The serializer separates blocks and text with
    newlines, so only newlines are stripped from the ends of each text segment;
    other whitespace, such as code indentation, is kept. Empty text is omitted.
    """
    segments: list[str | ReasoningCapsule] = []
    pos = 0
    for block in _think_blocks(content):
        if text := content[pos : block.start].strip("\n"):
            segments.append(text)
        segments.append(block.capsule)
        pos = block.end
    if text := content[pos:].strip("\n"):
        segments.append(text)
    return segments


class _ThinkBlock(NamedTuple):
    start: int
    end: int
    capsule: ReasoningCapsule


def _think_blocks(content: str) -> Iterator[_ThinkBlock]:
    """Yield each <think/> block in order, with its span and parsed capsule.

    A block whose `internal` attribute `decode_internal` rejects is literal
    text (e.g. a model quoting the marker): it is skipped whole and the search
    continues after it.
    """
    for match in _think_matches(content):
        try:
            internal = _parse_internal_attr(match.group(1))
        except NotInternal:
            continue

        attrs_str = match.group(1)
        reasoning = match.group(2).strip()

        # Parse attributes from opening tag
        signature = _parse_attr(attrs_str, "signature")
        redacted = _parse_attr(attrs_str, "redacted") == "true"

        # Extract nested <summary> tag from content
        reasoning, summary = _parse_summary(reasoning)
        if redacted and signature and signature.startswith("rs_"):
            # Redacted reasoning bodies carry opaque provider payloads such as
            # encrypted_content. Some text scaffolds wrap long lines; whitespace
            # inserted there is not part of the payload.
            reasoning = re.sub(r"\s+", "", reasoning)

        start, end = match.span()

        yield _ThinkBlock(
            start=start,
            end=end,
            capsule=ReasoningCapsule(
                reasoning=reasoning,
                signature=signature,
                redacted=redacted,
                summary=summary,
                internal=internal,
            ),
        )


_THINK_TAG = re.compile(r"<think([^>]*)>|</think>")
_THINK_BLOCK_BALANCED = re.compile(r"<think([^>]*)>(.*)</think>", re.DOTALL)
_THINK_BLOCK_FIRST_CLOSE = re.compile(r"<think([^>]*)>(.*?)</think>", re.DOTALL)


def _think_matches(content: str) -> Iterator[re.Match[str]]:
    """Yield each candidate <think/> block, searching after the previous one.

    A block runs from the first opening tag to its balanced closing tag or,
    when it has none, to the first closing tag after it.

    The text may come from a model or a sandboxed agent, so this runs in time
    linear in its length (it is called on the event loop): each region is
    scanned once, and the openings without a balanced close are recorded the
    first time a scan reaches the end of the text rather than found again by
    rescanning from each later opening.
    """
    # Every block ends with "</think>", so nothing after the last one matters.
    # Stopping there also keeps `[^>]*` from scanning to the end of the text
    # from each "<think" that has no ">".
    last_close = content.rfind("</think>")
    if last_close == -1:
        return
    tags_end = last_close + len("</think>")
    # Sorted starts of openings with no balanced close, once known (an array
    # rather than a list or set keeps this to 8 bytes per opening).
    unbalanced: array[int] | None = None
    offset = 0
    while (start := content.find("<think", offset, tags_end)) != -1:
        match: re.Match[str] | None = None
        if unbalanced is None or not _sorted_contains(unbalanced, start):
            opens = array("q")
            for tag in _THINK_TAG.finditer(content, start, tags_end):
                if tag.group(1) is not None:
                    opens.append(tag.start())
                    continue
                opens.pop()
                if not opens:
                    match = _THINK_BLOCK_BALANCED.match(content, start, tag.end())
                    break
            else:
                unbalanced = opens
        if match is None:
            # If this opening has no closing tag after it, neither does a later one.
            match = _THINK_BLOCK_FIRST_CLOSE.match(content, start)
            if match is None:
                return
        yield match
        offset = match.end()


def _sorted_contains(values: Sequence[int], value: int) -> bool:
    i = bisect_left(values, value)
    return i < len(values) and values[i] == value


def _parse_attr(attrs_str: str, name: str) -> str | None:
    """Extract attribute value from attributes string.

    Values are HTML-unescaped to invert the escaping done by
    `reasoning_to_think_tag`. Plain values without entities are
    unchanged.
    """
    match = re.search(rf'{name}="([^"]*)"', attrs_str)
    return html.unescape(match.group(1)) if match else None


def _parse_internal_attr(attrs_str: str) -> JsonValue | None:
    """Decode the internal attribute value (base64-encoded JSON), if present.

    Raises:
        NotInternal: The attribute does not carry internal state.
    """
    raw = _parse_attr(attrs_str, "internal")
    return None if raw is None else decode_internal(raw)


_SUMMARY = re.compile(r"<summary>(.*?)</summary>\s*", re.DOTALL)


def _parse_summary(content: str) -> tuple[str, str | None]:
    """Extract <summary> tag from content, returning (remaining_content, summary)."""
    # If the first opening has no closing tag after it, neither does a later
    # one, so match only there rather than searching (which would rescan to
    # the end of the text from every opening).
    start = content.find("<summary>")
    match = _SUMMARY.match(content, start) if start != -1 else None
    if match:
        summary = match.group(1)
        remaining = content[: match.start()] + content[match.end() :]
        return remaining.strip(), summary
    return content, None


def reasoning_to_think_tag(reasoning: "ContentReasoning") -> str:
    """Convert ContentReasoning to a <think> tag string for text-based serialization.

    This is the inverse of parse_content_with_reasoning - it takes a ContentReasoning
    object and produces a <think> tag string that can be embedded in plain text messages
    for agent bridge scenarios.
    """
    attribs = ""
    if reasoning.signature is not None:
        # HTML-escape so signatures containing quotes, '<', '>', or '&'
        # (e.g. OpenRouter's reasoning-details JSON payload) survive the
        # attribute round-trip. Plain signatures are unchanged.
        signature = html.escape(reasoning.signature, quote=True)
        attribs = f'{attribs} signature="{signature}"'
    if reasoning.redacted:
        attribs = f'{attribs} redacted="true"'
    if reasoning.internal is not None:
        # Base64 encode the JSON for safe embedding in an attribute
        internal_json = json.dumps(reasoning.internal)
        internal_b64 = base64.b64encode(internal_json.encode("utf-8")).decode("ascii")
        attribs = f'{attribs} internal="{internal_b64}"'

    inner = ""
    if reasoning.summary is not None:
        inner = f"<summary>{reasoning.summary}</summary>\n"
    inner = f"{inner}{reasoning.reasoning}"

    return f"<think{attribs}>\n{inner}\n</think>"


def emulate_reasoning_history(messages: list[ChatMessage]) -> list[ChatMessage]:
    emulated_messages: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, ChatMessageAssistant) and isinstance(
            message.content, list
        ):
            content: list[Content] = []
            for c in message.content:
                if isinstance(c, ContentReasoning):
                    content.append(ContentText(text=reasoning_to_think_tag(c)))
                else:
                    content.append(c)
            message = message.model_copy(update={"content": content})

        emulated_messages.append(message)
    return emulated_messages
