import type { ComponentType } from "react";
import type { BlocklistViewPayload } from "./types";
import type { WantedViewPayload } from "./wantedTypes";
import type { QueueViewPayload } from "./queueTypes";
import type { HistoryViewPayload } from "./historyTypes";
import type { ManualReviewViewPayload } from "./manualReviewTypes";
import type { SeriesViewPayload } from "./seriesTypes";
import type { SeriesDetailPayload } from "./SeriesDetail";
import type { ReliabilityViewPayload } from "./reliabilityTypes";

// Which payload belongs to which section, in one place the compiler reads.
//
// The registry used to be Record<string, ComponentType<{payload: Record<string,
// unknown>}>>, with every real component cast in through `unknown`:
//
//   wanted: Wanted as unknown as SectionComponent,
//   queue: Queue as unknown as SectionComponent,
//
// Each of those casts is individually reasonable -- a component's props really
// are narrower than the generic payload -- and collectively they meant the map
// had no opinion at all. Registering Queue under "wanted" compiled. So did
// registering a component whose payload shares no field with the section's,
// because `unknown` erases the question before it is asked.
//
// Strict mode is on and cannot help at a boundary that has been cast through
// unknown; this is the declaration that gives it something to check.
export type SectionPayloads = {
  __scaffold__: Record<string, unknown>;
  source_memory: BlocklistViewPayload;
  wanted: WantedViewPayload;
  queue: QueueViewPayload;
  history: HistoryViewPayload;
  manual_review: ManualReviewViewPayload;
  series: SeriesViewPayload;
  series_detail: SeriesDetailPayload;
  reliability: ReliabilityViewPayload;
};

export type SectionKey = keyof SectionPayloads;

/** A component that renders exactly the payload its section is served. */
export type SectionComponentFor<K extends SectionKey> = ComponentType<{
  payload: SectionPayloads[K];
}>;

/**
 * A loader that resolves to a component rendering exactly its section's payload.
 *
 * The registry holds loaders rather than components because each section is a
 * dynamic import, so that the bundler can give it its own chunk and a page can
 * stop downloading the eight sections it is not showing. The type check is the
 * same one and is made at the same place: a loader that resolves to another
 * section's component is a compile error at the line that registers it.
 */
export type SectionLoaderFor<K extends SectionKey> = () => Promise<{
  default: SectionComponentFor<K>;
}>;

/**
 * The registry, keyed so each entry must match its own section's payload.
 *
 * A mapped type rather than an index signature: an index signature would take
 * any string key back, which is the hole this exists to close.
 */
export type SectionRegistry = {
  [K in SectionKey]: SectionLoaderFor<K>;
};

/**
 * Identity at runtime; a type assertion at build time.
 *
 * Every entry in the literal passed here is checked against its own key's
 * payload, so a mismatched registration is a compile error at the line that
 * makes it rather than a render-time crash in front of an operator.
 */
export function defineSections(registry: SectionRegistry): SectionRegistry {
  return registry;
}

export function isSectionKey(value: string, registry: SectionRegistry): value is SectionKey {
  return Object.prototype.hasOwnProperty.call(registry, value);
}
