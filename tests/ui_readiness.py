"""Named, bounded UI barriers; mounting alone does not establish geometry."""
import asyncio


async def await_laid_out(widget, *, hit_test=False, timeout=5):
    name = f'layout:{widget.id or type(widget).__name__}'
    ready = asyncio.Event()
    active = True

    def check():
        if not active or not widget.is_attached:
            return
        region = widget.region
        valid = bool(region) and bool(widget.screen.region)
        if hit_test and valid:
            try:
                valid = (region.offset in widget.screen.region
                         and widget.app.get_widget_at(*region.offset)[0] is widget)
            except Exception as error:
                from textual.errors import NoWidget
                if not isinstance(error, NoWidget):
                    raise
                valid = False
        if valid:
            ready.set()
        else:
            widget.screen.call_after_refresh(check)
            widget.screen.refresh(layout=True)

    widget.screen.call_after_refresh(check)
    widget.screen.refresh(layout=True)
    try:
        await asyncio.wait_for(ready.wait(), timeout)
    except TimeoutError as error:
        raise AssertionError(f'{name} did not become ready within {timeout}s') from error
    finally:
        active = False


async def await_selection_settled(app, job, attempt, *, timeout=5):
    name = f'selection:{job}/{attempt}'
    ready = asyncio.Event()
    active = True

    def check():
        if not active:
            return
        selected = app.selected()
        snapshot = app.progress_snapshot
        if (selected and selected['id'] == job and selected['attempt_id'] == attempt
                and snapshot and snapshot.context.job_id == job
                and snapshot.context.attempt_id == attempt and not app.progress_busy):
            ready.set()
        else:
            if not app.progress_busy:
                app.collect_progress()
            app.call_after_refresh(check)
            app.refresh()

    check()
    try:
        await asyncio.wait_for(ready.wait(), timeout)
        await await_laid_out(app.screen_stack[0].query_one('#table'), timeout=timeout)
    except TimeoutError as error:
        raise AssertionError(f'{name} did not become ready within {timeout}s') from error
    finally:
        active = False
