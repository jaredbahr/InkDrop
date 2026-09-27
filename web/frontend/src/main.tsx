import { createRoot, type Root } from "react-dom/client";
import { Suspense, lazy, type ComponentType } from "react";
import { SectionErrorBoundary } from "./SectionErrorBoundary";
import { defineSections, isSectionKey, type SectionKey } from "./sections/sectionRegistry";
import { releaseViewportFill, trackViewportFill } from "./viewportFill";

// Bridge between the existing vanilla-JS shell (inkdrop_web.py's inline
// renderInkdropSection) and React. The shell owns navigation, the section
// chrome (title/meta/filters), and fetching each section's JSON payload; it
// hands a target element and that payload to InkDropReact.mount() for
// whichever section keys have been migrated. Everything else keeps rendering
// through the shell's existing vanilla-JS path untouched.
//
// A section entry here is intentionally just a component function: it owns
// nothing about routing or data-fetching beyond what the shell passes in.

type SectionPayload = Record<string, unknown>;
type AnySectionComponent = ComponentType<{ payload: unknown }>;

// Keyed, so each entry is checked against its own section's payload -- and
// held as a loader, so each section is its own chunk.
//
// The registry used to be Record<string, ComponentType<{payload: Record<string,
// unknown>}>> with every component cast in through `unknown`. Each cast was
// individually reasonable and collectively they meant the map had no opinion:
// registering Queue under "wanted" compiled, and so did a component sharing no
// field with the section's payload, because `unknown` erases the question
// before the compiler asks it. defineSections() is identity at runtime and an
// assertion at build time -- see sectionRegistry.ts. With loaders the same
// assertion is made one level down, on what the loader resolves to, so no cast
// is needed here at all.
//
// The loaders are what let the bundler split the sections apart. They were
// static imports, so opening any one page downloaded, parsed and evaluated all
// of them: one 356.24 kB (104.14 kB gzip at level 9) bundle whatever you were
// looking at. Opening Blocklist now fetches 66.45 kB gzip; Series, the largest
// at 80.82 kB because it carries react-virtuoso, is paid for only by someone
// who opens Series.
const SECTION_LOADERS = defineSections({
  // Filled in as each page migrates. The scaffold section exists only to
  // prove the build/serve/mount pipeline end-to-end and stays registered as
  // a harness for whatever page migrates next. It is loaded the same way as
  // everything else, so it proves chunk delivery too.
  __scaffold__: () => import("./ScaffoldPlaceholder").then((m) => ({ default: m.ScaffoldPlaceholder })),
  source_memory: () => import("./sections/Blocklist").then((m) => ({ default: m.Blocklist })),
  wanted: () => import("./sections/Wanted").then((m) => ({ default: m.Wanted })),
  queue: () => import("./sections/Queue").then((m) => ({ default: m.Queue })),
  history: () => import("./sections/History").then((m) => ({ default: m.History })),
  manual_review: () => import("./sections/ManualReview").then((m) => ({ default: m.ManualReview })),
  series: () => import("./sections/Series").then((m) => ({ default: m.Series })),
  // series_detail is mounted directly by renderInkdropSeriesDetailPage into
  // its own container (not through renderInkdropSection's generic view-key
  // gate the way "series" is) -- the vanilla page still owns the toolbar and
  // commandbar around it. See SeriesDetail.tsx's file comment.
  series_detail: () => import("./sections/SeriesDetail").then((m) => ({ default: m.SeriesDetail })),
  reliability: () => import("./sections/ReliabilityView").then((m) => ({ default: m.ReliabilityView })),
});

const CHUNK_RELOAD_KEY = "inkdrop-react-chunk-reload";

// A chunk that will not load is almost always a deployment that replaced it
// while this document was open: the browser still holds the old entry (cached
// under its own ?v=) and asks for a hash that is no longer on disk. One reload
// fetches the new entry and the names it refers to. The sessionStorage guard
// makes it exactly one, so a chunk that is genuinely missing surfaces as an
// error instead of reloading forever -- and it surfaces into the section's own
// error boundary below, which is why that is the right place for it to stop.
function withStaleChunkRecovery<T>(load: () => Promise<T>): () => Promise<T> {
  return () =>
    load().then(
      (module) => {
        try {
          window.sessionStorage.removeItem(CHUNK_RELOAD_KEY);
        } catch {
          // Private mode or blocked storage. Nothing to clear, nothing to do.
        }
        return module;
      },
      (error) => {
        let alreadyReloaded = true;
        try {
          alreadyReloaded = window.sessionStorage.getItem(CHUNK_RELOAD_KEY) === "1";
          if (!alreadyReloaded) window.sessionStorage.setItem(CHUNK_RELOAD_KEY, "1");
        } catch {
          // Without storage there is no way to bound the retry, so do not retry.
          alreadyReloaded = true;
        }
        if (!alreadyReloaded) window.location.reload();
        throw error;
      },
    );
}

// lazy() is memoised per section: building it inside the render would produce a
// new component type every time and remount the section on each render.
const lazySections = new Map<SectionKey, AnySectionComponent>();

function sectionComponent(sectionKey: SectionKey): AnySectionComponent {
  let component = lazySections.get(sectionKey);
  if (!component) {
    // defineSections already checked, at the line that registers it, that this
    // loader resolves to the component for this key. The widening here is what
    // lets one map hold all nine: an indexed read gives the union of the nine
    // loader types, and no single component type describes that union. It is
    // the same boundary cast mount() makes below, for the same reason -- the
    // payload arrives as parsed JSON -- and it erases nothing the registry had
    // not already proved.
    const loader = SECTION_LOADERS[sectionKey] as () => Promise<{ default: AnySectionComponent }>;
    component = lazy(withStaleChunkRecovery(loader));
    lazySections.set(sectionKey, component);
  }
  return component;
}

const roots = new WeakMap<Element, Root>();

function mount(sectionKey: string, container: Element, payload: SectionPayload): boolean {
  if (!isSectionKey(sectionKey, SECTION_LOADERS)) return false;
  const Component = sectionComponent(sectionKey);
  let root = roots.get(container);
  if (!root) {
    root = createRoot(container);
    roots.set(container, root);
  }
  // Publish how much room this container actually has before the bottom of the
  // window. Sections with a self-scrolling pane size off it; the rest ignore
  // it. See viewportFill.ts for why it is measured rather than assumed.
  trackViewportFill(container);
  // Every island renders inside a boundary, so a render-time throw costs the
  // operator that one section rather than the whole workspace. The payload is
  // the reset token: the shell hands down a fresh one on each navigation and
  // each reload, which is exactly when a section that failed deserves another
  // attempt. See SectionErrorBoundary.tsx for what it does and does not catch.
  //
  // Suspense sits INSIDE the boundary so that a chunk which cannot be fetched
  // lands in it too: React re-throws a rejected lazy import during render, and
  // a section that cannot load is exactly the failure the boundary exists for.
  // Its fallback is empty on purpose -- the shell has already put its own
  // loading chrome in this container, and replacing it with a second spinner
  // for the few milliseconds a chunk takes would be a flash, not feedback.
  //
  // mount() is still synchronous and still returns true here: the shell's
  // contract is "this section key is mine to render", not "the pixels are on
  // screen".
  root.render(
    <SectionErrorBoundary sectionKey={sectionKey} resetToken={payload}>
      <Suspense fallback={null}>
        <Component payload={payload} />
      </Suspense>
    </SectionErrorBoundary>,
  );
  return true;
}

function unmount(container: Element): void {
  releaseViewportFill(container);
  const root = roots.get(container);
  if (!root) return;
  root.unmount();
  roots.delete(container);
}

function hasSection(sectionKey: string): boolean {
  // Synchronous, as the shell needs it: the answer is "is this key mine",
  // which the registry knows without loading anything.
  return isSectionKey(sectionKey, SECTION_LOADERS);
}

declare global {
  interface Window {
    InkDropReact?: {
      mount: typeof mount;
      unmount: typeof unmount;
      hasSection: typeof hasSection;
    };
  }
}

window.InkDropReact = { mount, unmount, hasSection };
