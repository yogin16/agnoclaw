"""
Example: Custom Tools Integration

Demonstrates:
- Creating a custom @tool function
- Creating a custom Toolkit class
- Adding custom tools to AgentHarness
- Per-run tool restrictions via skills
"""

from agno.tools import tool, Toolkit
from agnoclaw import AgentHarness


# ── Custom @tool function ─────────────────────────────────────────────────

@tool(name="word_count")
def word_count(text: str) -> dict:
    """
    Count words, sentences, and characters in text.

    Args:
        text: The text to analyze.

    Returns:
        Dict with word_count, sentence_count, char_count.
    """
    words = text.split()
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    return {
        "word_count": len(words),
        "sentence_count": len(sentences),
        "char_count": len(text),
        "avg_words_per_sentence": round(len(words) / max(len(sentences), 1), 1),
    }


@tool(name="hex_encode")
def hex_encode(text: str) -> str:
    """
    Encode a string to hexadecimal.

    Args:
        text: The string to encode.

    Returns:
        Hex-encoded string.
    """
    return text.encode("utf-8").hex()


# ── Custom Toolkit class ──────────────────────────────────────────────────

class TextAnalysisToolkit(Toolkit):
    """A toolkit for text analysis operations."""

    def __init__(self):
        super().__init__(name="text_analysis")
        self.register(word_count)
        self.register(hex_encode)
        self.register(self.summarize_stats)

    def summarize_stats(self, text: str, label: str = "Document") -> str:
        """
        Print a formatted summary of text statistics.

        Args:
            text: The text to analyze.
            label: Label to use in the summary.

        Returns:
            Formatted summary string.
        """
        stats = word_count(text)
        lines = [
            f"## {label} Statistics",
            f"- Words: {stats['word_count']}",
            f"- Sentences: {stats['sentence_count']}",
            f"- Characters: {stats['char_count']}",
            f"- Avg words/sentence: {stats['avg_words_per_sentence']}",
        ]
        return "\n".join(lines)


# ── Use with AgentHarness ─────────────────────────────────────────────────

toolkit = TextAnalysisToolkit()

agent = AgentHarness(
    name="TextAnalyst",
    extra_tools=[toolkit],
)

# The agent now has access to word_count, hex_encode, and summarize_stats
result = agent.run(
    "Analyze this text: 'The quick brown fox jumps over the lazy dog. "
    "It was a sunny day. Everyone was happy.'"
)
print(result.content)

# ── Agent with only specific tools (no default tools) ─────────────────────
# You can override defaults entirely for restricted agents

from agnoclaw.tools import make_bash_tool

minimal_agent = AgentHarness(
    name="MinimalAgent",
    extra_tools=[word_count],
    # Disable defaults via config
    config=None,  # uses defaults — but you can pass a custom HarnessConfig
)

result2 = minimal_agent.run("Count words in: 'Hello world this is a test'")
print("\n" + result2.content)
