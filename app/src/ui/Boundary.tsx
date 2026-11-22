// Keeps one broken figure or section from blanking the whole page.
import { Component, ReactNode } from "react";

export class Boundary extends Component<{ children: ReactNode; label?: string }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: String((e as Error)?.message ?? e) };
  }
  render() {
    if (this.state.error) {
      return <div className="error">{this.props.label ?? "This part of the page"} couldn't be drawn: {this.state.error}</div>;
    }
    return this.props.children;
  }
}
