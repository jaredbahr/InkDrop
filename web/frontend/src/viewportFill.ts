// How tall may an island's scroll pane be before it runs off the bottom of the
// window?
//
// The islands mount into #inkdropSectionRows, the last child of the section
// panel. Everything above it -- masthead, stat cards, command bar, the Series
// library toolbar, an alert banner -- is vanilla shell chrome whose height
// nobody here controls: it changes with the viewport (the toolbar wraps), with
// the data (stat cards appear once counts load) and with the page state (a
// banner). The first version of the Series virtual scroller guessed that chrome
// at a flat `calc(100vh - 220px)`. Measured live it is ~104px, so the pane ran
// ~150px past the fold at 900px tall, the document grew a second scrollbar, and
// the row straddling the fold was left sliced in half.
//
// So measure it instead of guessing. Both numbers come off the live box:
//   top      -- where the rows box actually starts, chrome included
//   trailing -- whatever renders after it (the content shell's bottom gutter),
//               subtracted so the pane stops short of the document bottom and
//               no page-level scrollbar appears behind the pane's own
//
// The result is published as a custom property on the mount container, so any
// island that needs a self-scrolling pane can size itself off it in CSS rather
// than inventing its own viewport arithmetic. Sections that just page their
// rows read it and ignore it.

const FILL_PROPERTY = "--inkdrop-island-fill-height";

// Below this a pane is too short to be worth virtualizing, and something about
// the measurement is more likely wrong than the layout genuinely being that
// cramped. Matches the min-height the stylesheet carries for the same reason.
const MIN_FILL_PX = 320;

// Sub-pixel churn from zoom levels and fractional layout is not worth a write.
const WRITE_EPSILON_PX = 1;

type Tracker = {
  measure: () => void;
  dispose: () => void;
};

const trackers = new WeakMap<Element, Tracker>();

// The highest ancestor that is still a real content box -- body's direct
// child, i.e. `main` in this shell. Walking to it rather than naming a class
// keeps this working for any container the shell hands an island.
function outermostPageBlock(container: HTMLElement): HTMLElement {
  let block = container;
  while (block.parentElement && block.parentElement !== document.body && block.parentElement !== document.documentElement) {
    block = block.parentElement;
  }
  return block;
}

function availableHeight(container: HTMLElement): number | null {
  const rect = container.getBoundingClientRect();
  // Hidden section (the shell keeps every panel in the DOM and toggles
  // `hidden`): nothing meaningful to measure, and writing a bogus value now
  // would stick until the next resize.
  if (rect.width === 0 && rect.height === 0) return null;

  // Whatever renders after this box inside the page column: the content
  // shell's bottom gutter, the app shell's own, plus any section still
  // expanded below the panel. Subtracting it is what keeps the pane from
  // pushing the document past the fold and growing a second, page-level
  // scrollbar behind the pane's own.
  //
  // Measured against the outermost page block (`main`), not against
  // documentElement.scrollHeight and not against `body`. Both of those are
  // clamped to the viewport -- scrollHeight by definition, body by its
  // `min-height: 100vh` -- so once the document is shorter than the window the
  // trailing they imply silently absorbs every pixel of slack. Any height then
  // measures as already correct, including a collapsed one, which is a stable
  // fixed point the pane can never grow back out of. `main` is content-sized,
  // so the gap below us depends only on what is below us.
  const trailing = Math.max(0, outermostPageBlock(container).getBoundingClientRect().bottom - rect.bottom);

  // The value is consumed by the container's children, so it has to be room in
  // the container's *content* box: the rows box carries a 24px bottom gutter of
  // its own on narrow viewports, and a pane sized to the border box overflows
  // by exactly that much.
  const style = getComputedStyle(container);
  const ownInsets =
    (parseFloat(style.paddingTop) || 0) +
    (parseFloat(style.paddingBottom) || 0) +
    (parseFloat(style.borderTopWidth) || 0) +
    (parseFloat(style.borderBottomWidth) || 0);

  // Document coordinates, not viewport ones: the answer is "how much room is
  // there below this box when the page is at the top", which must not change
  // as the user scrolls.
  const documentTop = rect.top + window.scrollY;

  // Converges rather than oscillating: no term moves when our own write
  // changes the container's height.
  return Math.max(MIN_FILL_PX, Math.round(window.innerHeight - documentTop - trailing - ownInsets));
}

// Keep the mount container's fill height in step with the chrome above it.
// Idempotent per container: re-mounting a section reuses the existing tracker
// and just re-measures.
export function trackViewportFill(container: Element): void {
  if (!(container instanceof HTMLElement)) return;

  const existing = trackers.get(container);
  if (existing) {
    existing.measure();
    return;
  }

  let lastWritten: number | null = null;
  let frame = 0;

  const measure = () => {
    const next = availableHeight(container);
    if (next === null) return;
    if (lastWritten !== null && Math.abs(next - lastWritten) < WRITE_EPSILON_PX) return;
    lastWritten = next;
    container.style.setProperty(FILL_PROPERTY, `${next}px`);
  };

  // Chrome above the rows box settles over several frames after a mount (fonts,
  // stat cards, the toolbar's own layout), so re-measure on the next frame as
  // well as right now.
  const scheduleMeasure = () => {
    if (frame) return;
    frame = window.requestAnimationFrame(() => {
      frame = 0;
      measure();
    });
  };

  measure();
  scheduleMeasure();

  window.addEventListener("resize", scheduleMeasure);

  // The chrome above can change height without the window resizing -- a banner
  // appears, the toolbar wraps, counts land. Observing the panel catches all of
  // those; our own writes settle in one pass because of the convergence above.
  const observed = container.closest(".core-panel") || container.parentElement;
  const observer = typeof ResizeObserver === "function" && observed
    ? new ResizeObserver(scheduleMeasure)
    : null;
  if (observer && observed) observer.observe(observed);

  trackers.set(container, {
    measure,
    dispose: () => {
      if (frame) window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", scheduleMeasure);
      observer?.disconnect();
      container.style.removeProperty(FILL_PROPERTY);
      trackers.delete(container);
    },
  });
}

export function releaseViewportFill(container: Element): void {
  trackers.get(container)?.dispose();
}
