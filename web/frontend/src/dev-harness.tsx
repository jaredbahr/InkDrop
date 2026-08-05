import "./main";

// `npm run dev` entry only — registers window.InkDropReact (via ./main) then
// immediately mounts the scaffold section into the dev harness page so a
// component can be iterated on without booting the Python server. Swap the
// section key/payload here while building a real page's component.
const root = document.getElementById("dev-harness-root");
if (root) {
  window.InkDropReact?.mount("__scaffold__", root, { rows: [1, 2, 3] });
}
