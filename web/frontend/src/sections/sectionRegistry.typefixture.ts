// Compile-time assertions for the section registry and the shell bridge.
//
// This file contains no runtime assertions and is never imported. It is
// checked by `tsc --noEmit`, which the build runs before Vite, and every
// claim below is stated as `@ts-expect-error` -- a directive that is itself
// an error when the line it guards compiles cleanly. So a regression that
// re-opens one of these holes does not merely fail to be caught: it turns
// this file red.
//
// The holes are the ones the registry used to have. Before defineSections(),
// every component was cast into the map through `unknown`, which erases the
// question before the compiler asks it, so registering Queue under "wanted"
// compiled and so did a component sharing no field with the section payload.

import type { SectionComponentFor, SectionLoaderFor, SectionPayloads } from "./sectionRegistry";
import type { ManualSearchTarget, SeriesNavTarget } from "../shellBridge";
import { Wanted } from "./Wanted";
import { Queue } from "./Queue";

// --- the registry -----------------------------------------------------------

// The control, first: a section's own component really does fit its own key.
// Without this, every negative below would be satisfied by a map that accepts
// nothing at all.
const correct: SectionComponentFor<"wanted"> = Wanted;
void correct;

// @ts-expect-error a Queue component must not be registrable as the Wanted section
const swapped: SectionComponentFor<"wanted"> = Queue;
void swapped;

// @ts-expect-error and not the other way round either
const swappedBack: SectionComponentFor<"queue"> = Wanted;
void swappedBack;

// The registry holds loaders now, one per section, so that each section is its
// own chunk. The same claim has to hold one level down: resolving to another
// section's component must not satisfy a key.
const correctLoader: SectionLoaderFor<"wanted"> = () => Promise.resolve({ default: Wanted });
void correctLoader;

// @ts-expect-error a loader resolving to Queue must not satisfy the Wanted section
const swappedLoader: SectionLoaderFor<"wanted"> = () => Promise.resolve({ default: Queue });
void swappedLoader;

// A payload is not interchangeable with another section's payload.
declare const wantedPayload: SectionPayloads["wanted"];
declare const queuePayload: SectionPayloads["queue"];

// @ts-expect-error a Queue payload is not a Wanted payload
const mismatchedPayload: SectionPayloads["wanted"] = queuePayload;
void mismatchedPayload;

void wantedPayload;

// @ts-expect-error a section key the registry does not declare is not a key
type Unknown = SectionPayloads["not_a_real_section"];
declare const unknownSection: Unknown;
void unknownSection;

// --- the shell bridge -------------------------------------------------------

// Every bridge member is optional, because the shell is a separate script
// that may not have wired one yet. Treating one as guaranteed -- which is
// what SeriesDetail.tsx's own declaration used to do for openDetail -- is the
// claim about load order that nothing enforces.
declare const nav: NonNullable<Window["InkDropSeriesNav"]>;

// @ts-expect-error openDetail may be absent, so it cannot be called unguarded
nav.openDetail({ series_id: "s1" });

// The control: guarded, it is fine.
nav.openDetail?.({ series_id: "s1" });

// A target type describes what the shell's handler actually reads, so any
// row carrying those fields is acceptable and nothing needs a cast.
const target: ManualSearchTarget = { series_id: "s1", issue_number: 4 };
void target;
const navTarget: SeriesNavTarget = { series: "Some Series" };
void navTarget;

// @ts-expect-error but a field the handler never reads is still not part of the contract
const wrongTarget: SeriesNavTarget = { series_id: "s1", nonsense_field: true };
void wrongTarget;
