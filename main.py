"""
Denisse — a personal AI assistant CLI.

Run:
    python main.py

Type a message and press Enter. Type 'exit' or 'quit' to stop.
Conversation history persists to memory/conversation.json between runs.
"""

import sys

from brain import Brain
import memory_store
from safety import confirm_risky_tool


def main():
    print("Denisse is online. Type 'exit' to quit.\n")

    try:
        brain = Brain()
    except RuntimeError as e:
        print(f"Startup error: {e}")
        sys.exit(1)

    messages = memory_store.load_history()

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye.")
            break
        if user_input.lower() == "/clear":
            messages = []
            memory_store.clear_history()
            print("(memory cleared)")
            continue

        messages.append({"role": "user", "content": user_input})

        try:
            reply, messages = brain.respond(messages, confirm=confirm_risky_tool)
        except Exception as e:
            print(f"Error: {e}")
            continue

        print(f"denisse> {reply}\n")
        memory_store.save_history(messages)


if __name__ == "__main__":
    main()
