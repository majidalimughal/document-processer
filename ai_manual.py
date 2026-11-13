from ai_service import summarize_document

text = """
Our company generated $2 million in revenue this year.
Customer retention increased by 15%.
The company plans to expand into two new markets next year.
"""

summary = summarize_document(text)
print(summary)
