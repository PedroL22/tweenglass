import type { TweenGlassApi } from "../shared/protocol";

declare global {
  interface Window {
    tweenglass: TweenGlassApi;
  }
}
