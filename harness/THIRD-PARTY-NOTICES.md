# Third-party components

OIF's own source is licensed under Apache-2.0. Bundled dependencies retain their own licenses.

- **CPython 3.12.14**, built by the [python-build-standalone project](https://github.com/astral-sh/python-build-standalone), is redistributed with its license and notices in the `python` directory. CPython uses the Python Software Foundation license and includes additional third-party notices.
- **Microsoft.Web.WebView2 SDK 1.0.4191.47**: the loader and managed assemblies are redistributed with `desktop/WebView2-LICENSE.txt`. The separately installed Evergreen Runtime is provided by Microsoft under its own terms.
- **Python dependencies** are enumerated with exact versions and integrity values in `uv.lock` and `requirements-windows.txt`. Their original metadata and license files are included under `python/Lib/site-packages/*.dist-info/`. These include LangGraph, Deep Agents, LangChain, FastAPI, Uvicorn, HTTPX, Pydantic and pypdf, with their transitive dependencies. Pytest is included for the controlled verification path.

The release does not bundle a model, provider account, Docker Desktop or a WebView2 runtime installer. Provider services and optional tools have separate terms.
