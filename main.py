"""
Denisse — a personal AI assistant CLI.

Run:
    python main.py

Type a message and press Enter. Type 'exit' or 'quit' to stop.
Conversation history persists to memory/conversation.json between runs.
"""

import sys

import conversation
from safety import confirm_risky_tool


def main():
    print("Denisse is online. Type 'exit' to quit.\n")

    try:
        brain = conversation.get_brain()
    except RuntimeError as e:
        print(f"Startup error: {e}")
        sys.exit(1)

    messages = conversation.replay()

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
            archived = conversation.reset()
            print(f"(memory cleared, archived to {archived})" if archived else "(memory cleared)")
            continue

        turn_start = len(messages)
        messages.append({"role": "user", "content": user_input})

        try:
            reply, messages = brain.respond(messages, confirm=confirm_risky_tool)
        except Exception as e:
            print(f"Error: {e}")
            continue

        print(f"denisse> {reply}\n")
        conversation.record(messages[turn_start:])


if __name__ == "__main__":
    main()
