# Each pattern matches the LAST `ANSWER:` occurrence in the output (models
# sometimes self-correct, and CoT prompts instruct the answer on the last
# line). LETTER and WORD use a negative lookahead with DOTALL to skip earlier
# occurrences; LINE is anchored to end-of-string with \Z.
ANSWER_PATTERN_LETTER = r"(?is)ANSWER\s*:\s*([A-Za-z])(?:[^\w]|\n|$)(?!.*ANSWER\s*:)"
ANSWER_PATTERN_WORD = (
    r"(?is)ANSWER\s*:\s*(\S+?)(?=[.,;:!?]?\s*(?:\n|$))(?!.*ANSWER\s*:)"
)
# LINE captures the same answer as `ANSWER\s*:\s*([^\n]+)\s*\Z` in linear
# time. That form backtracked cubically on long whitespace runs and rescanned
# the line for every marker on it. Here no two adjacent quantifiers can take
# the same whitespace, and each line start tries only its first and last
# marker (any other marker on the line succeeds or fails with the first), so
# the match begins at the start of the marker's line.
ANSWER_PATTERN_LINE = (
    r"(?im)^(?>[^\n]*?ANSWER\s*:)(?>[^\n]*ANSWER\s*:)??"
    r"\s*(\S[^\n]*+|[^\S\n](?=\n*+\Z))\s*+\Z"
)
