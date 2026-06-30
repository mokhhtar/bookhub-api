"""
Centralized prompt templates for all Gemini calls.
Keeping these in one file makes them easy to tune without touching route logic.
"""

DEPTH_INSTRUCTIONS = {
    "quick": "Write ONE concise paragraph (60-90 words). Just the core premise and takeaway.",
    "medium": "Write 3 short sections with bold headers: **Premise**, **Key Ideas**, **Takeaway**. "
               "Each section 2-4 sentences. Use markdown bold for headers.",
    "deep": "Write a detailed summary with these markdown sections: "
            "**Overview** (2-3 sentences), **Main Arguments** (4-6 bullet points), "
            "**Key Quotes or Examples** (if well-known), **Who Should Read This** (1-2 sentences), "
            "**Critical Takeaway** (1-2 sentences). Be substantive, 250-400 words total.",
}


def summary_prompt(title: str, author: str, depth: str) -> str:
    author_part = f" by {author}" if author else ""
    instruction = DEPTH_INSTRUCTIONS.get(depth, DEPTH_INSTRUCTIONS["quick"])
    return f"""You are a literary analyst writing for a book-tools website.

Book: "{title}"{author_part}

{instruction}

Rules:
- If you don't recognize this exact book, say so honestly in one sentence rather than inventing details.
- Do not include the book title or author name in your response — just the summary content.
- No preamble like "Here is a summary". Start directly with the content.
- Write in clear, engaging prose. Avoid generic filler phrases.
- Use markdown **bold** for any section headers, but no headers level 1-3 (#)."""


def questions_prompt(title: str, author: str) -> str:
    author_part = f" by {author}" if author else ""
    return f"""You are preparing discussion questions for a book club.

Book: "{title}"{author_part}

Generate exactly 10 thoughtful, open-ended discussion questions about this book.
Mix question types: 3-4 about themes/ideas, 2-3 about characters/narrative choices,
2-3 that connect the book to the reader's own life or current events,
1-2 that are slightly provocative or debate-sparking.

Rules:
- If you don't recognize this exact book, generate questions based on the title/genre as honestly as possible, framed generally.
- Return ONLY a JSON array of 10 strings, nothing else. No markdown, no preamble.
- Each question should be one sentence, 15-30 words.
- Example format: ["Question one?", "Question two?", ...]"""


def recommend_prompt(books: str, mood: str) -> str:
    mood_part = f"\nThe reader's current mood/preference: {mood}" if mood else ""
    return f"""You are a book recommendation expert.

The reader loves these books: {books}{mood_part}

Recommend exactly 5 books that this reader would likely enjoy, based on themes,
writing style, genre, and emotional tone — not just surface-level genre matching.

Rules:
- Do not recommend any book already in their list.
- Prefer well-known, real, published books only.
- Return ONLY a JSON array of 5 objects, nothing else. No markdown, no preamble.
- Each object: {{"title": "...", "author": "...", "reason": "one sentence, 12-20 words, specific to why it matches"}}
- Example format: [{{"title": "Book Name", "author": "Author Name", "reason": "Because..."}}]"""


def similar_prompt(title: str, genre: str) -> str:
    genre_part = f" (genre: {genre})" if genre else ""
    return f"""You are a book recommendation expert.

Find books similar to: "{title}"{genre_part}

Recommend exactly 4 similar books — similar themes, style, or audience.

Rules:
- Do not include the original book in the results.
- Prefer well-known, real, published books only.
- Return ONLY a JSON array of 4 objects, nothing else. No markdown, no preamble.
- Each object: {{"title": "...", "author": "..."}}
- Example format: [{{"title": "Book Name", "author": "Author Name"}}]"""


def compare_prompt(book_a: str, book_b: str) -> str:
    return f"""You are a literary analyst comparing two books for readers deciding between them.

Book A: "{book_a}"
Book B: "{book_b}"

Compare them across these dimensions: Genre, Writing Style, Difficulty Level,
Pacing, Target Audience, Emotional Tone.

Rules:
- If either book is unrecognized, be honest and compare based on available knowledge.
- Return ONLY a JSON object, nothing else. No markdown, no preamble.
- Format exactly:
{{
  "rows": [
    {{"label": "Genre", "a": "...", "b": "..."}},
    {{"label": "Writing Style", "a": "...", "b": "..."}},
    {{"label": "Difficulty", "a": "...", "b": "..."}},
    {{"label": "Pacing", "a": "...", "b": "..."}},
    {{"label": "Best For", "a": "...", "b": "..."}},
    {{"label": "Tone", "a": "...", "b": "..."}}
  ],
  "verdict": "2-3 sentence honest comparison helping the reader choose between them."
}}
Keep each cell value short: 3-8 words."""
