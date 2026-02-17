import sys
import click
from rich.console import Group
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from unshackle.core.console import console

"""
Select module for unshackle
Author: CodeName393
==========================
[Acknowledgment]
The interactive selection concept and user interface design of this module 
were inspired by the 'beaupy' library (MIT License).
(https://github.com/petereon/beaupy)

[Note]
1. This code is an original implementation written from scratch and does not contain source code from the 'beaupy' library.
2. Parts of the implementation in this module were developed with the assistance of AI.
"""

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS: import msvcrt

class Selector:
    """
    A custom interactive selector class using the Rich library.
    Allows for multi-selection of items with pagination and collapsible headers.
    """

    def __init__(
        self,
        options: list[str],
        cursor_style: str = "pink",
        text_style: str = "text",
        page_size: int = 8,
        minimal_count: int = 0,
        dependencies: dict[int, list[int]] = None,
        collapse_on_start: bool = False
    ):
        """
        Initialize the Selector.

        Args:
            options: List of strings to select from.
            cursor_style: Rich style for the highlighted cursor item.
            text_style: Rich style for normal items.
            page_size: Number of items to show per page.
            minimal_count: Minimum number of items that must be selected.
            dependencies: Dictionary mapping parent index to list of child indices.
            collapse_on_start: If True, child items are hidden initially.
        """
        self.options = options
        self.cursor_style = cursor_style
        self.text_style = text_style
        self.page_size = page_size
        self.minimal_count = minimal_count
        self.dependencies = dependencies or {}
        
        # Parent-Child mapping for quick lookup
        self.child_to_parent = {}
        for parent, children in self.dependencies.items():
            for child in children:
                self.child_to_parent[child] = parent

        self.cursor_index = 0
        self.selected_indices = set()
        self.scroll_offset = 0

        # Tree view state
        self.expanded_headers = set()
        if not collapse_on_start:
            # Expand all by default
            self.expanded_headers.update(self.dependencies.keys())

    def get_visible_indices(self) -> list[int]:
        """
        Returns a sorted list of indices that should be currently visible.
        A child is visible only if its parent is in self.expanded_headers.
        """
        visible = []
        for idx in range(len(self.options)):
            # If it's a child, check if parent is expanded
            if idx in self.child_to_parent:
                parent = self.child_to_parent[idx]
                if parent in self.expanded_headers:
                    visible.append(idx)
            else:
                # It's a header or independent item, always visible
                visible.append(idx)
        return visible

    def get_renderable(self):
        """
        Constructs and returns the renderable object (Table + Info) for the current state.
        """
        visible_indices = self.get_visible_indices()
        
        # Adjust scroll offset to ensure cursor is visible
        if self.cursor_index not in visible_indices:
            # Fallback if cursor got hidden (should be handled in move, but safety check)
            self.cursor_index = visible_indices[0] if visible_indices else 0

        try:
            cursor_visual_pos = visible_indices.index(self.cursor_index)
        except ValueError:
            cursor_visual_pos = 0
            self.cursor_index = visible_indices[0]

        # Calculate logical page start/end based on VISIBLE items
        start_idx = self.scroll_offset
        end_idx = start_idx + self.page_size
        
        # Dynamic scroll adjustment
        if cursor_visual_pos < start_idx:
            self.scroll_offset = cursor_visual_pos
        elif cursor_visual_pos >= end_idx:
            self.scroll_offset = cursor_visual_pos - self.page_size + 1
        
        # Re-calc render range
        render_indices = visible_indices[self.scroll_offset : self.scroll_offset + self.page_size]

        table = Table(show_header=False, show_edge=False, box=None, pad_edge=False, padding=(0, 1, 0, 0))
        table.add_column("Indicator", justify="right", no_wrap=True)
        table.add_column("Option", overflow="ellipsis", no_wrap=True)

        for idx in render_indices:
            option = self.options[idx]
            is_cursor = idx == self.cursor_index
            is_selected = idx in self.selected_indices

            symbol = "[X]" if is_selected else "[ ]"
            style = self.cursor_style if is_cursor else self.text_style
            indicator_text = Text(f"{symbol}", style=style)

            content_text = Text.from_markup(f"{option}")
            content_text.style = style

            table.add_row(indicator_text, content_text)

        # Fill empty rows to maintain height
        rows_rendered = len(render_indices)
        for _ in range(self.page_size - rows_rendered):
            table.add_row(Text(" "), Text(" "))

        total_visible = len(visible_indices)
        total_pages = (total_visible + self.page_size - 1) // self.page_size
        if total_pages == 0: total_pages = 1
        current_page = (self.scroll_offset // self.page_size) + 1

        if self.dependencies:
            info_text = Text(
                f"\n[Space]: Toggle  [a]: All  [e]: Fold/Unfold  [E]: All Fold/Unfold\n[Enter]: Confirm  [↑/↓]: Move  [←/→]: Page  (Page {current_page}/{total_pages})",
                style="gray",
            )
        else:
            info_text = Text(
                f"\n[Space]: Toggle  [a]: All  [←/→]: Page  [Enter]: Confirm  (Page {current_page}/{total_pages})",
                style="gray",
            )

        return Padding(Group(table, info_text), (0, 5))

    def move_cursor(self, delta: int):
        """
        Moves the cursor up or down through VISIBLE items only.
        """
        visible_indices = self.get_visible_indices()
        if not visible_indices:
            return

        try:
            current_visual_idx = visible_indices.index(self.cursor_index)
        except ValueError:
            current_visual_idx = 0

        new_visual_idx = (current_visual_idx + delta) % len(visible_indices)
        self.cursor_index = visible_indices[new_visual_idx]

    def change_page(self, delta: int):
        """
        Changes the current page view by the specified delta (previous/next page).
        """
        visible_indices = self.get_visible_indices()
        if not visible_indices:
            return

        total_visible = len(visible_indices)
        
        # Calculate current logical page
        current_page = self.scroll_offset // self.page_size
        total_pages = (total_visible + self.page_size - 1) // self.page_size
        
        new_page = current_page + delta

        if 0 <= new_page < total_pages:
            self.scroll_offset = new_page * self.page_size
            
            # Move cursor to top of new page
            try:
                # Calculate what visual index corresponds to the start of the new page
                new_visual_cursor = self.scroll_offset
                if new_visual_cursor < len(visible_indices):
                    self.cursor_index = visible_indices[new_visual_cursor]
                else:
                    self.cursor_index = visible_indices[-1]
            except IndexError:
                pass

    def toggle_selection(self):
        """
        Toggles the selection state of the item currently under the cursor.
        """
        target_indices = {self.cursor_index}

        if self.cursor_index in self.dependencies:
            target_indices.update(self.dependencies[self.cursor_index])

        should_select = self.cursor_index not in self.selected_indices

        if should_select:
            self.selected_indices.update(target_indices)
        else:
            self.selected_indices.difference_update(target_indices)
            
    def toggle_expand(self, expand: bool = None):
        """
        Expands or collapses the current header.
        Args:
            expand: True to expand, False to collapse, None to toggle.
        """
        if self.cursor_index in self.dependencies:
            if expand is None:
                if self.cursor_index in self.expanded_headers:
                    self.expanded_headers.remove(self.cursor_index)
                else:
                    self.expanded_headers.add(self.cursor_index)
            elif expand:
                self.expanded_headers.add(self.cursor_index)
            else:
                if self.cursor_index in self.expanded_headers:
                    self.expanded_headers.remove(self.cursor_index)

    def toggle_expand_all(self):
        """
        Toggles expansion state of ALL headers.
        If all are expanded -> Collapse all.
        Otherwise -> Expand all.
        """
        if not self.dependencies:
            return
        all_headers = set(self.dependencies.keys())
        if self.expanded_headers == all_headers:
            self.expanded_headers.clear()
        else:
            self.expanded_headers = all_headers.copy()

    def toggle_all(self):
        """
        Toggles the selection of all items.
        """
        if len(self.selected_indices) == len(self.options):
            self.selected_indices.clear()
        else:
            self.selected_indices = set(range(len(self.options)))

    def get_input_windows(self):
        """
        Captures and parses keyboard input on Windows systems using msvcrt.
        Returns command strings like 'UP', 'DOWN', 'ENTER', etc.
        """
        key = msvcrt.getch()
        # Ctrl+C (0x03) or ESC (0x1b)
        if key == b"\x03" or key == b"\x1b":
            return "CANCEL"
        # Special keys prefix (Arrow keys, etc., send 0xe0 or 0x00 first)
        if key == b"\xe0" or key == b"\x00":
            try:
                key = msvcrt.getch()
                if key == b"H": return "UP" # Arrow Up
                if key == b"P": return "DOWN" # Arrow Down
                if key == b"K": return "LEFT" # Arrow Left
                if key == b"M": return "RIGHT" # Arrow Right
            except Exception:
                pass

        try:
            char = key.decode("utf-8", errors="ignore")
        except Exception:
            return None

        if char in ("\r", "\n"): return "ENTER"
        if char == " ": return "SPACE"
        if char in ("q", "Q"): return "QUIT"
        if char in ("a", "A"): return "ALL"
        if char == "e": return "EXPAND"
        if char == "E": return "EXPAND_ALL"
        if char in ("w", "W", "k", "K"): return "UP"
        if char in ("s", "S", "j", "J"): return "DOWN"
        if char in ("h", "H"): return "LEFT"
        if char in ("d", "D", "l", "L"): return "RIGHT"
        return None

    def get_input_unix(self):
        """
        Captures and parses keyboard input on Unix/Linux systems using click.getchar().
        Returns command strings like 'UP', 'DOWN', 'ENTER', etc.
        """
        char = click.getchar()
        # Ctrl+C
        if char == "\x03": return "CANCEL"
        
        # ANSI Escape Sequences for Arrow Keys
        mapping = {
            "\x1b[A": "UP", # Escape + [ + A
            "\x1b[B": "DOWN", # Escape + [ + B
            "\x1b[C": "RIGHT", # Escape + [ + C
            "\x1b[D": "LEFT", # Escape + [ + D
        }
        if char in mapping: return mapping[char]
        
        # Handling manual Escape sequences
        if char == "\x1b": # ESC
            try:
                next1 = click.getchar()
                if next1 in ("[", "O"): # Sequence indicators
                    next2 = click.getchar()
                    if next2 == "A": return "UP" # Arrow Up
                    if next2 == "B": return "DOWN" # Arrow Down
                    if next2 == "C": return "RIGHT" # Arrow Right
                    if next2 == "D": return "LEFT" # Arrow Left
                return "CANCEL"
            except Exception:
                return "CANCEL"

        if char in ("\r", "\n"): return "ENTER"
        if char == " ": return "SPACE"
        if char in ("q", "Q"): return "QUIT"
        if char in ("a", "A"): return "ALL"
        if char == "e": return "EXPAND"
        if char == "E": return "EXPAND_ALL"
        if char in ("w", "W", "k", "K"): return "UP"
        if char in ("s", "S", "j", "J"): return "DOWN"
        if char in ("h", "H"): return "LEFT"
        if char in ("d", "D", "l", "L"): return "RIGHT"
        return None

    def run(self) -> list[int]:
        """
        Starts the main event loop for the selector.
        Renders the UI and processes input until confirmed or cancelled.

        Returns:
            list[int]: A sorted list of selected indices.
        """
        try:
            with Live(self.get_renderable(), console=console, auto_refresh=False, transient=True) as live:
                while True:
                    live.update(self.get_renderable(), refresh=True)
                    if IS_WINDOWS:
                        action = self.get_input_windows()
                    else:
                        action = self.get_input_unix()

                    if action == "UP":
                        self.move_cursor(-1)
                    elif action == "DOWN":
                        self.move_cursor(1)
                    elif action == "LEFT":
                        self.change_page(-1)
                    elif action == "RIGHT":
                        self.change_page(1)
                    elif action == "EXPAND":
                        self.toggle_expand(expand=None)
                    elif action == "EXPAND_ALL":
                        self.toggle_expand_all()
                    elif action == "SPACE":
                        self.toggle_selection()
                    elif action == "ALL":
                        self.toggle_all()
                    elif action in ("ENTER", "QUIT"):
                        if len(self.selected_indices) >= self.minimal_count:
                            return sorted(list(self.selected_indices))
                    elif action == "CANCEL":
                        raise KeyboardInterrupt
        except KeyboardInterrupt:
            return []


def select_multiple(
    options: list[str],
    minimal_count: int = 1,
    page_size: int = 8,
    return_indices: bool = True,
    cursor_style: str = "pink",
    collapse_on_start: bool = False,
    **kwargs,
) -> list[int]:
    """
    Drop-in replacement using custom Selector with global console.

    Args:
        options: List of options to display.
        minimal_count: Minimum number of selections required.
        page_size: Number of items per page.
        return_indices: If True, returns indices; otherwise returns the option strings.
        cursor_style: Style color for the cursor.
        collapse_on_start: If True, child items are hidden initially.
    """
    selector = Selector(
        options=options,
        cursor_style=cursor_style,
        text_style="text",
        page_size=page_size,
        minimal_count=minimal_count,
        collapse_on_start=collapse_on_start,
        **kwargs,
    )

    selected_indices = selector.run()

    if return_indices:
        return selected_indices
    return [options[i] for i in selected_indices]
