import { createRoot, type Root } from "react-dom/client";
import type { ComponentType } from "react";
import { ScaffoldPlaceholder } from "./ScaffoldPlaceholder";
import { Blocklist } from "./sections/Blocklist";
import { Wanted } from "./sections/Wanted";
import { Queue } from "./sections/Queue";
import { History } from "./sections/History";
import { ManualReview } from "./sections/ManualReview";
import { Series } from "./sections/Series";
import { SeriesDetail } from "./sections/SeriesDetail";
import { ReliabilityView } from "./sections/ReliabilityView";
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
type SectionComponent = ComponentType<{ payload: SectionPayload }>;

const SECTION_COMPONENTS: Record<string, SectionComponent> = {
  // Filled in as each page migrates. The scaffold section exists only to
  // prove the build/serve/mount pipeline end-to-end and stays registered as
  // a harness for whatever page migrates next.
  __scaffold__: ScaffoldPlaceholder,
  // Each real section component owns a narrower payload shape than
  // SectionPayload -- the cast here is the one place that gap is bridged,
  // rather than loosening every component's own prop types to match.
  source_memory: Blocklist as unknown as SectionComponent,
  wanted: Wanted as unknown as SectionComponent,
  queue: Queue as unknown as SectionComponent,
  history: History as unknown as SectionComponent,
  manual_review: ManualReview as unknown as SectionComponent,
  series: Series as unknown as SectionComponent,
  // series_detail is mounted directly by renderInkdropSeriesDetailPage into
  // its own container (not through renderInkdropSection's generic view-key
  // gate the way "series" is) -- the vanilla page still owns the toolbar and
  // commandbar around it. See SeriesDetail.tsx's file comment.
  series_detail: SeriesDetail as unknown as SectionComponent,
  reliability: ReliabilityView as unknown as SectionComponent,
};

const roots = new WeakMap<Element, Root>();

function mount(sectionKey: string, container: Element, payload: SectionPayload): boolean {
  const Component = SECTION_COMPONENTS[sectionKey];
  if (!Component) return false;
  let root = roots.get(container);
  if (!root) {
    root = createRoot(container);
    roots.set(container, root);
  }
  // Publish how much room this container actually has before the bottom of the
  // window. Sections with a self-scrolling pane size off it; the rest ignore
  // it. See viewportFill.ts for why it is measured rather than assumed.
  trackViewportFill(container);
  root.render(<Component payload={payload} />);
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
  return sectionKey in SECTION_COMPONENTS;
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
