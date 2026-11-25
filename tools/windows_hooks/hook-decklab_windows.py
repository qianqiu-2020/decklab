"""Keep DeckLab sources replaceable while PyInstaller analyzes their imports."""
module_collection_mode = {
    'decklab_windows': 'py',
    'decklab_demo': 'py',
    'decklab_core': 'py',
    'decklab_mcp': 'py',
    'decklabctl': 'py',
}
