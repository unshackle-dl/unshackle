import click
import sys
from rich.console import Group
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from unshackle.core.console import console

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import msvcrt

class Selector:
    """
    A custom interactive selector class using the Rich library.
    Allows for multi-selection of items with pagination.
    """
    def __init__(
        self,
        options: list[str],
        cursor_style: str = "pink",
        text_style: str = "text",
        page_size: int = 8,
        minimal_count: int = 0,
        dependencies: dict[int, list[int]] = None,
        prefixes: list[str] = None
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
        """
        self.options = options
        self.cursor_style = cursor_style
        self.text_style = text_style
        self.page_size = page_size
        self.minimal_count = minimal_count
        self.dependencies = dependencies or {}
        
        self.cursor_index = 0
        self.selected_indices = set()
        self.scroll_offset = 0

    def get_renderable(self):
        """
        Constructs and returns the renderable object (Table + Info) for the current state.
        """
        table = Table(show_header=False, show_edge=False, box=None, pad_edge=False, padding=(0, 1, 0, 0))
        table.add_column("Indicator", justify="right", no_wrap=True)
        table.add_column("Option", overflow="ellipsis", no_wrap=True)

        for i in range(self.page_size):
            idx = self.scroll_offset + i
            
            if idx < len(self.options):
                option = self.options[idx]
                is_cursor = (idx == self.cursor_index)
                is_selected = (idx in self.selected_indices)

                symbol = "[X]" if is_selected else "[ ]"
                style = self.cursor_style if is_cursor else self.text_style
                indicator_text = Text(f"{symbol}", style=style)

                content_text = Text.from_markup(option)
                content_text.style = style

                table.add_row(indicator_text, content_text)
            else:
                table.add_row(Text(" "), Text(" "))

        total_pages = (len(self.options) + self.page_size - 1) // self.page_size
        current_page = (self.scroll_offset // self.page_size) + 1
        
        info_text = Text(
            f"\n[Space]: Toggle  [a]: All  [←/→]: Page  [Enter]: Confirm  (Page {current_page}/{total_pages})", 
            style="gray"
        )
        
        return Padding(Group(table, info_text), (0, 5))

    def move_cursor(self, delta: int):
        """
        Moves the cursor up or down by the specified delta.
        Updates the scroll offset if the cursor moves out of the current view.
        """
        self.cursor_index = (self.cursor_index + delta) % len(self.options)
        new_page_idx = self.cursor_index // self.page_size
        self.scroll_offset = new_page_idx * self.page_size

    def change_page(self, delta: int):
        """
        Changes the current page view by the specified delta (previous/next page).
        Also moves the cursor to the first item of the new page.
        """
        current_page = self.scroll_offset // self.page_size
        total_pages = (len(self.options) + self.page_size - 1) // self.page_size
        new_page = current_page + delta

        if 0 <= new_page < total_pages:
            self.scroll_offset = new_page * self.page_size
            first_idx_of_page = self.scroll_offset
            if first_idx_of_page < len(self.options):
                self.cursor_index = first_idx_of_page
            else:
                self.cursor_index = len(self.options) - 1

    def toggle_selection(self):
        """
        Toggles the selection state of the item currently under the cursor.
        Propagates selection to children if defined in dependencies.
        """
        target_indices = {self.cursor_index}
        
        if self.cursor_index in self.dependencies:
            target_indices.update(self.dependencies[self.cursor_index])

        should_select = self.cursor_index not in self.selected_indices

        if should_select:
            self.selected_indices.update(target_indices)
        else:
            self.selected_indices.difference_update(target_indices)

    def toggle_all(self):
        """
        Toggles the selection of all items. 
        If all are selected, clears selection. Otherwise, selects all.
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
        if key == b'\x03' or key == b'\x1b':
            return 'CANCEL'
        if key == b'\xe0' or key == b'\x00':
            try:
                key = msvcrt.getch()
                if key == b'H': return 'UP'
                if key == b'P': return 'DOWN'
                if key == b'K': return 'LEFT'
                if key == b'M': return 'RIGHT'
            except: pass
        
        try: char = key.decode('utf-8', errors='ignore')
        except: return None
            
        if char in ('\r', '\n'): return 'ENTER'
        if char == ' ': return 'SPACE'
        if char in ('q', 'Q'): return 'QUIT'
        if char in ('a', 'A'): return 'ALL'
        if char in ('w', 'W', 'k', 'K'): return 'UP'
        if char in ('s', 'S', 'j', 'J'): return 'DOWN'
        if char in ('h', 'H'): return 'LEFT'
        if char in ('d', 'D', 'l', 'L'): return 'RIGHT'
        return None

    def get_input_unix(self):
        """
        Captures and parses keyboard input on Unix/Linux systems using click.getchar().
        Returns command strings like 'UP', 'DOWN', 'ENTER', etc.
        """
        char = click.getchar()
        if char == '\x03' or char == '\x1b':
            if char == '\x1b':
                return 'CANCEL'
            return 'CANCEL'
        if char in ('\r', '\n'): return 'ENTER'
        if char == ' ': return 'SPACE'
        if char in ('q', 'Q'): return 'QUIT'
        if char in ('a', 'A'): return 'ALL'
        if char in ('w', 'W', 'k', 'K'): return 'UP'
        if char in ('s', 'S', 'j', 'J'): return 'DOWN'
        if char in ('h', 'H'): return 'LEFT'
        if char in ('d', 'D', 'l', 'L'): return 'RIGHT'
        if char == '\x1b':
            try:
                next1 = click.getchar()
                if next1 in ['[', 'O']:
                    next2 = click.getchar()
                    if next2 == 'A': return 'UP'
                    if next2 == 'B': return 'DOWN'
                    if next2 == 'D': return 'LEFT'
                    if next2 == 'C': return 'RIGHT'
            except: pass
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
                    if IS_WINDOWS: action = self.get_input_windows()
                    else: action = self.get_input_unix()
                    
                    if action == 'UP': self.move_cursor(-1)
                    elif action == 'DOWN': self.move_cursor(1)
                    elif action == 'LEFT': self.change_page(-1)
                    elif action == 'RIGHT': self.change_page(1)
                    elif action == 'SPACE': self.toggle_selection()
                    elif action == 'ALL': self.toggle_all()
                    elif action in ('ENTER', 'QUIT'):
                        if len(self.selected_indices) >= self.minimal_count:
                            return sorted(list(self.selected_indices))
                    elif action == 'CANCEL': raise KeyboardInterrupt
        except KeyboardInterrupt:
            return []

def select_multiple(
    options: list[str],
    minimal_count: int = 1,
    page_size: int = 8,
    return_indices: bool = True,
    cursor_style: str = "pink",
    **kwargs
) -> list[int]:
    """
    Drop-in replacement using custom Selector with global console.
    
    Args:
        options: List of options to display.
        minimal_count: Minimum number of selections required.
        page_size: Number of items per page.
        return_indices: If True, returns indices; otherwise returns the option strings.
        cursor_style: Style color for the cursor.
    """
    selector = Selector(
        options=options,
        cursor_style=cursor_style,
        text_style="text",
        page_size=page_size,
        minimal_count=minimal_count,
        **kwargs
    )
    
    selected_indices = selector.run()
    
    if return_indices:
        return selected_indices
    return [options[i] for i in selected_indices]
