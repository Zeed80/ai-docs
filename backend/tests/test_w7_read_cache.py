"""W7: the read-cache must not hide a page that changed under the agent.

desktop_read/observe/tabs are grant-bound and carry identical arguments for
every page; the read-key made the product page answer "already_read" of the
search page, and the value was never read (live eval web-site-search-head).
"""

from __future__ import annotations


def test_browser_reads_are_not_deduped_by_arguments():
    import inspect

    from app.ai import agent_loop

    source = inspect.getsource(agent_loop.AgentSession._execute_single_tool)
    start = source.index("read_key = (")
    end = source.index("turn_reads =", start)
    block = source[start:end]
    assert 'original_name.split(".", 1)[0] != "computer_use"' in block
