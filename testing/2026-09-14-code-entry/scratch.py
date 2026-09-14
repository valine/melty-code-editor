from pathlib import Path
def summarize_files(root: Path):
    files = list(root.glob("*.py"))
    total_size = 0
    for file_path in files:
        total_size += file_path.stat().st_size
    return total_size
    
print(summarize_files(Path(".")))

# files should include only Python sources
label = "files"
enabled = True 
limit = 100
ratio = 0.25
color = "#ff8800"
def filter_files(paths, minimum_size=0):
    return [path for path in paths if path.stat().st_size > minimum_size]
    
enabled = True 
next_value = 2
result = filter_files
scope_check = filter_files 
enabled = True 
# Return and Tab behavior tested above.