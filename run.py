#!/usr/bin/env python3
"""
Simple runner script for the YouTube to PDF Telegram Bot
"""

import os
import sys

def main():
    """Main entry point for running the bot"""
    try:
        # Import and run the main bot
        from main import main as bot_main
        print("🤖 Starting YouTube to PDF Telegram Bot...")
        bot_main()
    except KeyboardInterrupt:
        print("\n⏹️  Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
