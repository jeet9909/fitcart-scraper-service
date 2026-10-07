import os

# The garment-facts step adds a Gemini text call before each image. Tests that count Gemini calls predate it,
# so it is off by default here; tests of the step turn it on explicitly.
os.environ.setdefault("GARMENT_FACTS_ENABLED", "false")
