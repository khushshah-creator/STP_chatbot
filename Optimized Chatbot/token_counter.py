import google.genai as genai
from dotenv import load_dotenv
import os
from prompts import INTENT_SYSTEM_PROMPT, SQL_SYSTEM_PROMPT, ANSWER_SYSTEM_PROMPT

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)


token_check = client.models.count_tokens(
    model="gemini-3.5-flash",
    contents=SQL_SYSTEM_PROMPT
)

print(f"Total tokens: {token_check.total_tokens}")