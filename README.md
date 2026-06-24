# mlx2ipynb

Convert MATLAB Live Script (`.mlx`) to Jupyter Notebook (`.ipynb`), MATLAB script (`.m`), or Markdown (`.md`).

## Usage

```bash
python mlx2ipynb.py file.mlx                     # all formats
python mlx2ipynb.py file.mlx -f ipynb            # notebook only
python mlx2ipynb.py file.mlx -f ipynb md         # selective
python mlx2ipynb.py file.mlx --forbid-html       # suppress HTML in output
```

## Output formats

| Flag      | Format           | Description                                                     |
| --------- | ---------------- | --------------------------------------------------------------- |
| `ipynb` | Jupyter Notebook | json based format with Markdown cells + MATLAB code cells and embedded image figure outputs |
| `m`     | MATLAB script    | Plain`.m` file with code cells only                           |
| `md`    | Markdown         | Rich text +`` ```matlab ``` `` code blocks                      |

## Formatting support

This script will try to use purely markdown when possible, but it may includes extra html when working with styles that are not supported in standard markdown.

| MLX feature                   | Output                                         |
| ----------------------------- | ---------------------------------------------- |
| Bold`                | `**bold**`                                   |
| Italic              | `*italic*`                                   |
| Bold + Italic                 | `***text***`                                 |
| Underline           | `<u>text</u>`                                |
| Monospace                     | `` `code` ``                                   |
| Mixed (e.g. bold+italic+mono) | `<strong><em><code>...</code></em></strong>` |
| Heading / Heading2 / …       | `##` / `###` / `####` / …               |
| Title                         | `#`                                          |
| Lists (bulleted / numbered)   | `-` / `1.`                                 |
| Alignment (center/right)      | `<div style="text-align:...">`               |
| Code cells                    | MATLAB code in `cddata` blocks                |
| Figure outputs                | Base64 PNG embedded in notebook                |
| Inline images                 | Base64-embedded (png)                          |
| Interative Components (Buttons and such)   | Not supported |
## Requires

- Python 3.12+
- No external dependencies (stdlib only: `zipfile`, `xml.etree.ElementTree`, `json`)
