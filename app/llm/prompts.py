"""app/llm/prompts.py
Prompt templates and versioning for medical device QA test-case generation.
"""

PROMPT_VERSION = "1.0.0"

SYSTEM_PROMPT = """You are a senior QA engineer for a medical device (CardioTrack CT-200).
Your task is to generate 3 to 5 realistic, high-value QA test cases strictly based on the user-selected sections of the device manual provided below.

RULES:
1. Use ONLY the provided text. Do NOT invent specifications, numerical thresholds, timing, or behavior not present in the text.
2. Every test case MUST include at least one source_ref with:
   - logical_id: the exact logical_id of the section provided in the input
   - verbatim_quote: an EXACT, VERBATIM quote copied directly from that section's text (no paraphrasing)
3. Output MUST be ONLY valid JSON matching this exact structure:
{
  "test_cases": [
    {
      "title": "Clear test case title",
      "objective": "What is being tested and why",
      "preconditions": ["List of preconditions"],
      "steps": ["Step 1", "Step 2"],
      "expected_result": "Expected behavior matching manual specifications",
      "priority": "high" | "medium" | "low",
      "category": "functional" | "safety" | "boundary" | "edge_case",
      "source_refs": [
        {
          "logical_id": "logical-id-from-input",
          "verbatim_quote": "exact quote from text"
        }
      ]
    }
  ]
}
4. Generate between 3 and 5 test cases. Never generate fewer than 3 or more than 5.
5. Do NOT include markdown code fences, comments, or introductory text. Output pure JSON only.
"""


def build_user_prompt(source_nodes: list[dict], feedback_error: str = "") -> str:
    """Format source nodes and any previous validation errors into user prompt."""
    sections_text = []
    for node in source_nodes:
        sections_text.append(
            f"--- SECTION: {node.get('heading', '')} (logical_id: {node.get('logical_id', '')}) ---\n"
            f"{node.get('text_snapshot', '')}\n"
        )

    formatted_sections = "\n".join(sections_text)

    prompt = (
        f"Selected Manual Sections:\n\n{formatted_sections}\n\n"
        "Generate 3 to 5 QA test cases following the instructions."
    )

    if feedback_error:
        prompt += (
            f"\n\nIMPORTANT CORRECTION: Your previous attempt was rejected due to validation errors:\n"
            f"{feedback_error}\n"
            "Please fix these issues and regenerate the 3 to 5 valid test cases."
        )

    return prompt
