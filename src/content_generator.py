import logging
import os

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a fintech professional writing LinkedIn posts that demonstrate genuine expertise, not content marketing.

Your voice is:
- Specific over vague (cite numbers, companies, timeframes)
- Skeptical over breathless (acknowledge limitations, question hype)
- Experienced over aspirational (write like you've shipped product, not read about it)
- Conversational but substantive (no fluff, but not academic either)

FORBIDDEN PHRASES (auto-fail if used):
- "Let that sink in" / "Read that again"
- "Here's what X taught me about Y"
- "I'm humbled/honored/excited to announce"
- "Unpopular opinion" / "Hot take" / "Controversial take"
- "This is fascinating" / "This resonates" / "This is striking"
- "Game-changer" / "Paradigm shift" / "Transformative"
- "The future of [industry] is..."
- "Thoughts?" as standalone ending
- Any emoji except when quoting someone who used one
- Starting with "AI is quietly..." or "X is reshaping..."

FORBIDDEN STRUCTURES:
- Rhetorical questions as substitutes for insights
- Lists of obvious observations (unless backed by data)
- False equivalencies between unrelated industries
- "This matters because..." without saying WHY it matters to the reader
- More than 3 hashtags total

REQUIRED ELEMENTS:
1. A specific, falsifiable claim in the first 2 sentences
2. At least one concrete data point, company name, or timeframe
3. Acknowledgment of uncertainty when speculating
4. A "so what" that isn't generic advice

STRUCTURE:
- First line: specific claim or surprising fact (not setup/context)
- Blank line
- 2-4 short paragraphs (2-3 sentences each) with blank lines between
- Blank line
- Source attribution at end (natural, not forced)
- Hashtags only if genuinely relevant (max 3)

TONE CALIBRATION:
- Write like you're explaining something interesting to a peer over coffee
- Assume your reader is smart and skeptical
- If you can't add original analysis, just summarize cleanly and ask a non-obvious question
- It's okay to say "I don't know" or "too early to tell"
- Never write anything you'd be embarrassed to have a CFO read

RED FLAGS YOU'RE DOING IT WRONG:
- If it sounds like it could be about any industry, you're too vague
- If you're using adjectives instead of specifics, you're hiding weak content
- If a 22-year-old growth hacker could've written it, start over
- If you remove the source and nothing remains, you didn't add value
"""

USER_PROMPT_TEMPLATE = """Source article:
Author: {author}
Publication: {source_name}
Title: {title}
URL: {url}

Key content:
{content_summary}

Generate a LinkedIn post (150-200 words) that:

1. Opens with the most surprising/specific fact from the source (NOT setup/context)
2. Explains why it matters using concrete examples or second-order implications
3. Acknowledges what's uncertain or overhyped if applicable
4. Ends with either:
   - A non-obvious question worth answering
   - A specific prediction with a timeframe
   - A clear "this matters if you're [specific role/situation]"

Attribution requirements:
- Mention author/source naturally in the flow (not as a citation)
- Link at the very end
- If you're mostly summarizing, be honest about that

Quality check before returning:
- Would this teach something to someone who read the original?
- Is there at least one claim I could argue with?
- Did I avoid thought-leader performance language?
- Would I send this to a colleague without cringing?

Return ONLY the post text."""


class ContentGenerator:
    """Generates LinkedIn post drafts using Claude API."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = model

    def generate_post(self, content_item: dict) -> dict:
        """Generate a LinkedIn post draft from a content item.

        Args:
            content_item: dict with keys: title, content, url, author,
                         source_name, source_type, category

        Returns:
            dict with: source_summary, commentary, full_post
        """
        prompt = USER_PROMPT_TEMPLATE.format(
            source_type=content_item.get("source_type", "article"),
            author=content_item.get("author") or content_item.get("source_name", "Unknown"),
            title=content_item.get("title", ""),
            url=content_item.get("url", ""),
            content_summary=_truncate(content_item.get("content", ""), 2000),
            source_name=content_item.get("source_name", ""),
        )

        logger.info("Generating post for: %s", content_item.get("title", "")[:80])

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            full_post = response.content[0].text.strip()

            # Build source summary
            author = content_item.get("author") or content_item.get("source_name", "")
            source_summary = f"Source: {author} — {content_item.get('title', '')}"
            if content_item.get("url"):
                source_summary += f"\nLink: {content_item['url']}"

            # Apply anti-cringe filter
            full_post = _anti_cringe_filter(full_post)

            return {
                "source_summary": source_summary,
                "commentary": full_post,
                "full_post": full_post,
            }

        except anthropic.APIError as e:
            logger.error("Claude API error: %s", e)
            raise

    def generate_batch(self, content_items: list[dict]) -> list[dict]:
        """Generate posts for multiple content items."""
        results = []
        for item in content_items:
            try:
                result = self.generate_post(item)
                result["content_item"] = item
                results.append(result)
            except Exception as e:
                logger.error("Failed to generate post for '%s': %s", item.get("title", ""), e)
        return results


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _anti_cringe_filter(text: str) -> str:
    """Remove common LinkedIn cringe patterns that slip through."""
    import re

    # Remove excessive emojis (keep max 1)
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF]+",
        flags=re.UNICODE,
    )
    emojis = emoji_pattern.findall(text)
    if len(emojis) > 1:
        # Keep only the first emoji
        for emoji in emojis[1:]:
            text = text.replace(emoji, "", 1)

    # Remove cringe phrases
    cringe_phrases = [
        r"(?i)let that sink in\.?",
        r"(?i)read that again\.?",
        r"(?i)i'll say it louder for the people in the back\.?",
        r"(?i)agree\?",
        r"(?i)thoughts\?\s*$",
    ]
    for pattern in cringe_phrases:
        text = re.sub(pattern, "", text)

    # Remove excessive hashtags (keep max 3)
    hashtags = re.findall(r"#\w+", text)
    if len(hashtags) > 3:
        for ht in hashtags[3:]:
            text = text.replace(ht, "", 1)

    # Clean up extra whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text
