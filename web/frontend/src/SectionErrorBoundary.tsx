import { Component, type ErrorInfo, type ReactNode } from "react";

// One island throwing must not take the workspace with it.
//
// mount() called root.render(<Component payload={payload} />) with nothing
// above it, so a render-time exception in any registered section unmounted
// that section's whole tree and left an empty container behind -- no message,
// no way back, and no indication that the shell around it (navigation, the
// other sections, the command bar) was still perfectly usable. The operator's
// only recovery was to guess that reloading the page might help.
//
// WHAT THIS DOES NOT CATCH, DELIBERATELY.
//   A React error boundary only sees errors thrown while rendering, in
//   lifecycle methods, and in constructors below it. It does not see rejected
//   promises, event-handler throws, or anything async -- those are the
//   request layer's job and are already surfaced as row failures and error
//   banners by useRowActions and each section's own loading state. Wrapping
//   islands here does not change that and must not be read as covering it.
//
// WHAT THE FALLBACK IS ALLOWED TO SAY.
//   The payload is the section's data: titles, paths, provider identifiers.
//   A thrown Error's message can quote any of it -- `Cannot read properties
//   of undefined (reading 'api_key')` is benign, but a hand-thrown
//   `new Error(\`rejected ${token}\`)` is not, and the boundary cannot tell
//   the two apart. So the fallback shows the error's *type* and a reference,
//   never its message and never the payload. The detail goes to the console,
//   which is the operator's own browser and already holds the stack anyway.

type Props = {
  sectionKey: string;
  // Changing this discards a recorded failure and re-renders the section.
  // The shell hands an island a new payload on every navigation and every
  // reload, so a section that failed on one page of data gets a clean attempt
  // at the next one rather than staying broken until the tab is reloaded.
  resetToken: unknown;
  children: ReactNode;
};

type State = {
  failed: boolean;
  errorName: string;
  reference: string;
  resetToken: unknown;
};

// Short, non-sequential, and generated in the browser: it correlates what the
// operator sees with the console entry beside it. It is not an identifier for
// anything on the server and is never sent anywhere.
function newReference(): string {
  return Math.random().toString(36).slice(2, 8).toUpperCase();
}

export class SectionErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, errorName: "", reference: "", resetToken: this.props.resetToken };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return {
      failed: true,
      errorName: error instanceof Error ? error.name : "Error",
      reference: newReference(),
    };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetToken === state.resetToken) return null;
    // New data for this section. Clear any recorded failure along with it --
    // `failed` is about the render that threw, not about the section.
    return { resetToken: props.resetToken, failed: false, errorName: "", reference: "" };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // Console only. The reference printed here is the one on screen, so an
    // operator reporting "section failed, reference 4K2XQ9" can be pointed at
    // the matching entry without anyone having to reproduce the failure.
    console.error(
      `InkDrop section "${this.props.sectionKey}" failed to render (reference ${this.state.reference})`,
      error,
      info.componentStack,
    );
  }

  private retry = (): void => {
    this.setState({ failed: false, errorName: "", reference: "" });
  };

  private reloadPage = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="section-error-boundary" role="alert">
        <h3>This section could not be displayed.</h3>
        <p>
          The rest of InkDrop is still working — use the navigation to go somewhere else, or try
          this section again.
        </p>
        <p className="section-error-boundary-detail">
          {this.state.errorName} · reference {this.state.reference} — the full detail is in your
          browser&rsquo;s developer console.
        </p>
        <div className="section-error-boundary-actions">
          <button type="button" className="primary" onClick={this.retry}>
            Try this section again
          </button>
          <button type="button" onClick={this.reloadPage}>
            Reload the page
          </button>
        </div>
      </div>
    );
  }
}
