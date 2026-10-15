"""Shared utility functions for modules."""

import socket
from typing import Optional


def get_primary_ip() -> Optional[str]:
    """Return the address of the interface used for outbound traffic.

    Connecting a UDP socket only selects a route; no packets are sent. Unlike
    resolving the hostname, this avoids loopback answers such as 127.0.1.1.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed publicly
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if address.startswith("127.") or address == "0.0.0.0" else address

def wrap_text(text: str, width: int = 42, indent: int = 0, preserve_line_breaks: bool = False) -> list[str]:
    """Wraps text to fit the printer width with optional indentation.
    
    Note: For most use cases, you should pass text directly to printer.print_body()
    or printer.print_text(), which will handle wrapping automatically using font
    metrics (more accurate than character-based wrapping). Use this function only
    when you need character-based pre-wrapping for specific layout requirements.
    
    Args:
        text: The text to wrap
        width: Maximum width for each line (in characters, approximate)
        indent: Number of spaces to indent (reduces available width)
        preserve_line_breaks: If True, preserves explicit line breaks from input
    """
    if preserve_line_breaks:
        # Split by newlines first to preserve explicit line breaks and empty lines
        input_lines = text.split('\n')
        all_lines = []
        
        for input_line in input_lines:
            # Check if this is an empty line (whitespace-only lines are also considered empty)
            words = input_line.split()
            
            # If empty line, preserve it for spacing
            if not words:
                all_lines.append("")
                continue
            
            # Wrap each non-empty line individually
            current_line = ""
            available_width = width - indent

            for word in words:
                if len(current_line) + len(word) + 1 <= available_width:
                    current_line += word + " "
                else:
                    if current_line:
                        all_lines.append(current_line.strip())
                    current_line = word + " "

            # Add the last line if there's content
            if current_line:
                all_lines.append(current_line.strip())
        
        return all_lines
    else:
        # Original behavior: split on all whitespace
        words = text.split()
        lines = []
        current_line = ""
        available_width = width - indent

        for word in words:
            if len(current_line) + len(word) + 1 <= available_width:
                current_line += word + " "
            else:
                if current_line:
                    lines.append(current_line.strip())
                current_line = word + " "

        if current_line:
            lines.append(current_line.strip())

        return lines


def wrap_text_pixels(
    text: str, font, max_width_pixels: int, default_font_size: int = 24
) -> list[str]:
    """Wrap text to fit within max_width_pixels using font metrics.

    Args:
        text: Text to wrap
        font: PIL ImageFont to measure text width
        max_width_pixels: Maximum width in pixels (accounting for margins)

    Returns:
        List of wrapped lines
    """
    if not text:
        return []

    # Account for left margin (2px) and right margin (2px)
    available_width = max_width_pixels - 4

    lines = []
    # Split by explicit newlines first
    input_paragraphs = text.split("\n")
    
    for paragraph in input_paragraphs:
        if not paragraph:
            # Preserve empty lines
            lines.append("")
            continue
            
        words = paragraph.split()
        current_line = ""

        for word in words:
            # Test if adding this word fits
            test_line = current_line + (" " if current_line else "") + word

            # Measure text width using font metrics
            if font:
                try:
                    # Use getbbox for accurate measurement (PIL 8.0+)
                    bbox = font.getbbox(test_line)
                    text_width = bbox[2] - bbox[0] if bbox else 0
                except AttributeError:
                    # Fallback for older PIL versions
                    try:
                        text_width = font.getlength(test_line)
                    except AttributeError:
                        text_width = len(test_line) * (getattr(font, "size", default_font_size)) * 0.6
            else:
                text_width = len(test_line) * default_font_size * 0.6

            if text_width <= available_width:
                current_line = test_line
            else:
                # Current line is full, start new line
                if current_line:
                    lines.append(current_line)

                # Measure word width to decide if we need to break it
                if font:
                    try:
                        bbox = font.getbbox(word)
                        word_width = bbox[2] - bbox[0] if bbox else 0
                    except AttributeError:
                        try:
                            word_width = font.getlength(word)
                        except AttributeError:
                            word_width = len(word) * (getattr(font, "size", default_font_size)) * 0.6
                else:
                    word_width = len(word) * default_font_size * 0.6

                # Only break words if they're longer than a full line
                if word_width > available_width:
                    # Word is too long for a single line, break it char by char
                    current_word = ""
                    for char in word:
                        test_char = current_word + char
                        if font:
                            try:
                                bbox = font.getbbox(test_char)
                                char_width = bbox[2] - bbox[0] if bbox else 0
                            except AttributeError:
                                char_width = len(test_char) * (getattr(font, "size", default_font_size)) * 0.6
                        else:
                            char_width = len(test_char) * default_font_size * 0.6

                        if char_width <= available_width:
                            current_word = test_char
                        else:
                            if current_word:
                                lines.append(current_word)
                            current_word = char
                    current_line = current_word
                else:
                    current_line = word

        if current_line:
            lines.append(current_line)

    return lines if lines else [""]
