// Stub for highlighter backends the viewer never selects (it uses the default "shiki-js").
const no = (name) => () => { throw new Error(`valet: the ${name} highlighter backend is not bundled`); };
export const init = no("highlights"), createHighlighter = no("highlights"), codeToTokens = no("highlights"),
  codeToHtml = no("highlights"), loadWasm = no("wasm"), createOnigurumaEngine = no("oniguruma");
export const isSupportedLanguage = () => false;
export const tokenNames = [];
export class LiveTokenizer { constructor() { no("highlights")(); } }
export class StreamTokenizer { constructor() { no("highlights")(); } }
export default no("wasm");
