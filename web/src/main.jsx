import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Volume2 } from 'lucide-react';
import './styles.css';

const API = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000';
const STATIC_DATA = import.meta.env.VITE_STATIC_DATA === '1';
const SEARCH_PLACEHOLDER = '天天中彩票';
const READING_LABELS = { pinyin: 'Pinyin', zhuyin: 'Zhuyin', jyutping: 'Jyutping', korean: 'Korean', japanese: 'Japanese' };

async function api(path, options) {
  const res = await fetch(`${API}${path}`, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

const jsonCache = new Map();
const textEncoder = new TextEncoder();

function dataUrl(path) {
  return `${import.meta.env.BASE_URL}${path}`;
}

async function fetchJson(path) {
  if (!jsonCache.has(path)) {
    jsonCache.set(path, fetch(dataUrl(path)).then((res) => {
      if (!res.ok) throw new Error(`Static data not found: ${path}`);
      return res.json();
    }));
  }
  return jsonCache.get(path);
}

function fnv1a32(value) {
  let hash = 0x811c9dc5;
  for (const byte of textEncoder.encode(value)) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

function hashShard(value) {
  return (fnv1a32(value) % 256).toString(16).padStart(2, '0');
}

async function staticSummary(text) {
  const shard = await fetchJson(`data/summaries/${hashShard(text)}.json`);
  return shard[text] || { text, has_character: 0, has_word: 0 };
}

async function staticEntry(text) {
  const shard = await fetchJson(`data/entries/${hashShard(text)}.json`);
  return shard[text] || {
    text,
    summary: await staticSummary(text),
    character: null,
    character_detail: null,
    words: [],
    word_readings: [],
    readings: [],
    characters: Array.from(text).length > 1 ? await Promise.all(Array.from(text).map(staticSummary)) : [],
    related_words: [],
    hsk: { word_levels: [], character_levels: [] },
  };
}

async function getEntryData(text) {
  if (STATIC_DATA) return staticEntry(text);
  return api(`/api/entry/${encodeURIComponent(text)}`);
}

function isHanChar(value) {
  return /\p{Script=Han}/u.test(value);
}

async function segmenterTerms(firstChar) {
  const shard = await fetchJson(`data/segmenter/${hashShard(firstChar)}.json`).catch(() => ({}));
  return shard[firstChar] || [];
}

async function segmentStatic(text) {
  const tokens = [];
  let i = 0;
  while (i < text.length) {
    const char = String.fromCodePoint(text.codePointAt(i));
    const charLength = char.length;
    if (!isHanChar(char)) {
      const start = i;
      i += charLength;
      while (i < text.length) {
        const next = String.fromCodePoint(text.codePointAt(i));
        if (isHanChar(next)) break;
        i += next.length;
      }
      tokens.push({ text: text.slice(start, i), start, end: i });
      continue;
    }

    const terms = await segmenterTerms(char);
    const match = terms.find((term) => text.startsWith(term, i));
    const tokenText = match || char;
    tokens.push({ text: tokenText, start: i, end: i + tokenText.length });
    i += tokenText.length;
  }

  const out = [];
  for (const token of tokens) {
    const summary = await staticSummary(token.text);
    if (Array.from(token.text).length > 1 && /\p{Script=Han}/u.test(token.text) && !summary.has_character && !summary.has_word) {
      let offset = token.start;
      for (const char of Array.from(token.text)) {
        out.push({ text: char, start: offset, end: offset + char.length, entry: await staticSummary(char) });
        offset += char.length;
      }
    } else {
      out.push({ ...token, entry: summary });
    }
  }
  return { text, tokens: out };
}

async function segmentText(text) {
  if (STATIC_DATA) return segmentStatic(text);
  return api('/api/segment', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
}

function useHashRoute() {
  const [hash, setHash] = useState(location.hash || '#/');
  useEffect(() => {
    const onHash = () => setHash(location.hash || '#/');
    addEventListener('hashchange', onHash);
    return () => removeEventListener('hashchange', onHash);
  }, []);
  return hash.slice(1);
}

function go(path) {
  location.hash = path;
}

function replaceHash(path) {
  history.replaceState(null, '', `${location.pathname}${location.search}#${path}`);
  dispatchEvent(new HashChangeEvent('hashchange'));
}

function readerPath(text, selectedEntry = '') {
  const base = `/read/${encodeURIComponent(text)}`;
  return selectedEntry ? `${base}/entry/${encodeURIComponent(selectedEntry)}` : base;
}

function asDefs(definitions) {
  return Array.isArray(definitions) ? definitions.join('; ') : '';
}

function isChineseToken(value) {
  return value && Array.from(value).every((char) => /\p{Script=Han}/u.test(char));
}

function speakChinese(value) {
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(value);
  utterance.lang = 'zh-CN';
  window.speechSynthesis.speak(utterance);
}

function translatorApi() {
  if (window.Translator?.create) {
    return {
      availability: (options) => window.Translator.availability?.(options) || Promise.resolve('available'),
      create: (options) => window.Translator.create(options),
    };
  }
  if (window.translation?.createTranslator) {
    return {
      availability: (options) => window.translation.canTranslate?.(options) || Promise.resolve('available'),
      create: (options) => window.translation.createTranslator(options),
    };
  }
  if (window.ai?.translator?.create) {
    return {
      availability: (options) => window.ai.translator.capabilities?.(options).then((cap) => cap.available) || Promise.resolve('available'),
      create: (options) => window.ai.translator.create(options),
    };
  }
  return null;
}

function availabilityAllowsTranslation(value) {
  if (!value) return true;
  if (typeof value === 'string') return value !== 'unavailable' && value !== 'no';
  if (typeof value === 'object') return value.available !== 'no' && value.available !== 'unavailable';
  return Boolean(value);
}

function progressPercent(event) {
  const loaded = Number(event.loaded);
  const total = Number(event.total);
  if (Number.isFinite(loaded) && Number.isFinite(total) && total > 0) return Math.min(100, (loaded / total) * 100);
  if (Number.isFinite(loaded) && loaded >= 0 && loaded <= 1) return loaded * 100;
  return null;
}

const TRANSLATOR_OPTIONS = { sourceLanguage: 'zh', targetLanguage: 'en' };
let sharedTranslator = null;
let sharedTranslatorPromise = null;
let sharedTranslatorApi = null;
let sharedTranslatorState = { supported: false, ready: false, progress: null };
const translatorListeners = new Set();

function setSharedTranslatorState(nextState) {
  sharedTranslatorState = { ...sharedTranslatorState, ...nextState };
  translatorListeners.forEach((listener) => listener(sharedTranslatorState));
}

function subscribeTranslator(listener) {
  translatorListeners.add(listener);
  listener(sharedTranslatorState);
  return () => translatorListeners.delete(listener);
}

function getSharedTranslator() {
  return sharedTranslator;
}

async function checkTranslatorAvailability() {
  const apiRef = sharedTranslatorApi || translatorApi();
  sharedTranslatorApi = apiRef;
  if (!apiRef) {
    setSharedTranslatorState({ supported: false, ready: false, progress: null });
    return false;
  }
  try {
    const availability = await apiRef.availability(TRANSLATOR_OPTIONS);
    const supported = availabilityAllowsTranslation(availability);
    setSharedTranslatorState({ supported, ready: Boolean(sharedTranslator), progress: null });
    return supported;
  } catch {
    setSharedTranslatorState({ supported: false, ready: false, progress: null });
    return false;
  }
}

function startTranslatorDownload() {
  const apiRef = sharedTranslatorApi || translatorApi();
  sharedTranslatorApi = apiRef;
  if (!apiRef) {
    setSharedTranslatorState({ supported: false, ready: false, progress: null });
    return Promise.resolve(null);
  }
  if (sharedTranslator) {
    setSharedTranslatorState({ supported: true, ready: true, progress: null });
    return Promise.resolve(sharedTranslator);
  }
  if (sharedTranslatorPromise) return sharedTranslatorPromise;
  setSharedTranslatorState({ supported: true, ready: false, progress: null });
  sharedTranslatorPromise = apiRef.create({
    ...TRANSLATOR_OPTIONS,
    monitor(monitor) {
      monitor.addEventListener?.('downloadprogress', (event) => {
        const nextProgress = progressPercent(event);
        if (nextProgress !== null) setSharedTranslatorState({ supported: true, ready: false, progress: nextProgress });
      });
    },
  }).then((translator) => {
    sharedTranslator = translator;
    setSharedTranslatorState({ supported: true, ready: true, progress: null });
    return translator;
  }).catch(() => {
    setSharedTranslatorState({ supported: true, ready: false, progress: null });
    return null;
  }).finally(() => {
    sharedTranslatorPromise = null;
  });
  return sharedTranslatorPromise;
}

function RubyText({ text, reading, mode = 'word' }) {
  if (!reading) return <span>{text}</span>;
  if (mode === 'chars') {
    const chars = Array.from(text);
    const parts = reading.trim().split(/\s+/);
    if (chars.length === parts.length) {
      return <span className="ruby-seq">{chars.map((char, i) => <span className="ruby-unit" key={`${char}-${i}`}><span className="ruby-rt">{parts[i]}</span><span className="ruby-rb">{char}</span></span>)}</span>;
    }
  }
  if (mode === 'word-wrap') {
    return <span className="word-ruby-wrap"><span className="word-ruby-reading">{reading}</span><span className="word-ruby-text">{Array.from(text).map((char, i) => <span key={`${char}-${i}`}>{char}</span>)}</span></span>;
  }
  return <ruby>{text}<rt>{reading}</rt></ruby>;
}

function SearchBox({ onResults }) {
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  async function submit(e) {
    e.preventDefault();
    const query = q.trim() || SEARCH_PLACEHOLDER;
    setLoading(true);
    try {
      startTranslatorDownload();
      go(readerPath(query));
      onResults([], query);
    } finally {
      setLoading(false);
    }
  }
  return <form className="search" onSubmit={submit}>
    <input value={q} onChange={e => setQ(e.target.value)} placeholder={SEARCH_PLACEHOLDER} />
    <button>{loading ? 'Going' : 'Go'}</button>
  </form>;
}

function EntryPage({ text, navigateEntry = (value) => go(`/entry/${encodeURIComponent(value)}`), className = 'detail' }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    setData(null); setErr('');
    getEntryData(text).then(setData).catch(e => setErr(e.message));
  }, [text]);
  useEffect(() => {
    if (data?.text) speakChinese(data.text);
  }, [data?.text]);
  if (err) return <main className="panel error">{err}</main>;
  if (!data) return <main className="panel">Loading...</main>;
  const charDetail = data.character_detail;
  const containingWords = (data.related_words || []).filter((w) => w.traditional !== text && w.simplified !== text);
  return <main className={className}>
    <section className="hero-entry">
      <div className="entry-title"><h1><RubyText text={text} reading={data.summary?.primary_pinyin} mode={text.length > 1 ? 'chars' : 'word'} /></h1><button className="speak-button" aria-label="Play reading" onClick={() => speakChinese(text)}><Volume2 size={20} /></button></div>
      <p>{data.summary?.english_summary || data.character?.definition || asDefs(data.words?.[0]?.definitions)}</p>
      <div className="chips">
        {data.summary?.hsk_min_level && <span>HSK {data.summary.hsk_min_level}</span>}
        {data.character && <span>{data.character.codepoint_hex}</span>}
      </div>
    </section>

    {data.character && <section className="section"><h2>Character</h2>
      <div className="kv">
        <span>Codepoint</span><b>{data.character.codepoint_hex}</b>
        <span>Block</span><b>{data.character.block}</b>
        <span>Strokes</span><b>{data.character.total_strokes || 'n/a'}</b>
        <span>Radical</span><b>{data.character.radical_strokes || 'n/a'}</b>
        <span>Definition</span><b>{data.character.definition}</b>
      </div>
    </section>}

    {text.length > 1 && <section className="section"><h2>Characters</h2>
      <div className="character-breakdown">{Array.from(text).map((ch, i) => {
        const charSummary = data.character_breakdown?.[i] || data.characters?.[i];
        return <button key={`${ch}-${i}`} onClick={() => navigateEntry(ch)}>
          <RubyText text={ch} reading={charSummary?.primary_pinyin || ''} />
        </button>;
      })}</div>
    </section>}

    {!!data.readings?.length && <section className="section"><h2>Readings</h2>
      <div className="reading-lines">{['pinyin', 'zhuyin', 'jyutping', 'korean'].map((system) => {
        const values = data.readings.filter((r) => r.system === system).map((r) => r.reading);
        return values.length ? <div key={system}><span>{READING_LABELS[system] || system}</span><b>{values.join(', ')}</b></div> : null;
      })}</div>
    </section>}

    {!!charDetail?.variant_display?.length && <section className="section"><h2>Variants</h2>
      <div className="variant-boxes">{charDetail.variant_display.map((v) => <button key={v.codepoint} onClick={() => navigateEntry(v.text)}>
        <span>{v.text}</span><small>{v.codepoint}</small>
      </button>)}</div>
    </section>}

    {!!containingWords.length && <section className="section"><h2>Containing Words</h2>
      <div className="word-grid containing-word-grid">{containingWords.map(w => <button key={w.id} onClick={() => navigateEntry(w.simplified || w.traditional)}><RubyText text={w.traditional} reading={w.pinyin_diacritic} mode="word-wrap" /></button>)}</div>
    </section>}
  </main>;
}

function ReaderPage({ initialText = '我想學中文', selectedEntry = '' }) {
  const [text, setText] = useState(initialText);
  const [data, setData] = useState(null);
  const [translatorState, setTranslatorState] = useState(sharedTranslatorState);
  const [translations, setTranslations] = useState({});
  useEffect(() => { setText(initialText); }, [initialText]);
  useEffect(() => {
    const unsubscribe = subscribeTranslator(setTranslatorState);
    checkTranslatorAvailability();
    return unsubscribe;
  }, []);
  const ensureTranslator = useCallback(() => {
    startTranslatorDownload();
  }, []);
  useEffect(() => {
    const handle = setTimeout(() => {
      segmentText(text).then(setData);
    }, 180);
    return () => clearTimeout(handle);
  }, [text]);
  const lines = useMemo(() => {
    const tokens = data?.tokens || [];
    const out = [];
    let current = [];
    let tokenIndex = 0;
    const isBreakChar = (char) => '\n.。!！?？'.includes(char);
    let i = 0;
    while (i < text.length) {
      while (tokenIndex < tokens.length && tokens[tokenIndex].start === i) {
        current.push(tokens[tokenIndex]);
        tokenIndex += 1;
      }
      if (isBreakChar(text[i])) {
        i += 1;
        while (i < text.length && isBreakChar(text[i])) {
          while (tokenIndex < tokens.length && tokens[tokenIndex].start === i) {
            current.push(tokens[tokenIndex]);
            tokenIndex += 1;
          }
          i += 1;
        }
        out.push(current);
        current = [];
        continue;
      }
      i += 1;
    }
    while (tokenIndex < tokens.length) {
      current.push(tokens[tokenIndex]);
      tokenIndex += 1;
    }
    if (current.length || out.length === 0) out.push(current);
    return out;
  }, [data, text]);
  const lineTexts = useMemo(() => lines.map((line) => line.map((token) => token.text).join('')), [lines]);
  useEffect(() => {
    const translator = getSharedTranslator();
    if (!translatorState.ready || !translator) {
      setTranslations({});
      return undefined;
    }
    let cancelled = false;
    setTranslations({});
    async function translateLines() {
      const nextTranslations = {};
      for (const [index, lineText] of lineTexts.entries()) {
        const source = lineText.trim();
        if (!source) continue;
        if (!/\p{Script=Han}/u.test(source)) {
          nextTranslations[index] = source;
          setTranslations({ ...nextTranslations });
          continue;
        }
        try {
          const translated = await translator.translate(source);
          if (cancelled) return;
          nextTranslations[index] = translated;
          setTranslations({ ...nextTranslations });
        } catch {
          if (cancelled) return;
          nextTranslations[index] = '';
          setTranslations({ ...nextTranslations });
        }
      }
    }
    translateLines();
    return () => {
      cancelled = true;
    };
  }, [lineTexts, translatorState.ready]);
  function updateText(value) {
    ensureTranslator();
    setText(value);
    replaceHash(readerPath(value, selectedEntry));
  }
  function selectEntry(value) {
    go(readerPath(text, value));
  }
  function closeEntry() {
    go(readerPath(text));
  }
  return <main className={selectedEntry ? 'reader-layout has-entry' : 'reader-layout'}>
    <section className="section reader-main" onPointerDownCapture={ensureTranslator}>
      <textarea value={text} onFocus={ensureTranslator} onChange={e => updateText(e.target.value)} />
      <div className="tokens">{lines.map((line, lineIndex) => {
        const lineText = line.map((token) => token.text).join('');
        const progressLabel = translatorState.progress === null ? '' : `Downloading translator ${translatorState.progress.toFixed(1)}%`;
        const translationText = translatorState.ready ? translations[lineIndex] : progressLabel;
        return <div className="token-line-block" key={lineIndex}>
          <div className="token-line">
            {line.map((t, i) => isChineseToken(t.text) ? <button key={`${lineIndex}-${i}-${t.start}`} onClick={() => selectEntry(t.text)}><RubyText text={t.text} reading={t.entry?.primary_pinyin || ''} mode={t.text.length > 1 ? 'chars' : 'word'} />{!t.entry?.primary_pinyin && <small>{t.entry?.english_summary}</small>}</button> : <span className="plain-token" key={`${lineIndex}-${i}-${t.start}`}>{t.text}</span>)}
            {!!lineText && <button className="line-speak" aria-label="Play line" onClick={() => speakChinese(lineText)}><Volume2 size={18} /></button>}
          </div>
          {translatorState.supported && <span className="line-translation">{translationText || '\u00a0'}</span>}
        </div>;
      })}</div>
    </section>
    {selectedEntry && <aside className="entry-aside">
      <button className="close-entry" aria-label="Close entry" onClick={closeEntry}>x</button>
      <EntryPage text={selectedEntry} navigateEntry={selectEntry} className="entry-pane" />
    </aside>}
  </main>;
}

function HskPage({ level }) {
  const [data, setData] = useState(null);
  useEffect(() => { api(level ? `/api/hsk/${level}` : '/api/hsk').then(setData); }, [level]);
  if (!data) return <main className="panel">Loading...</main>;
  if (!level) return <main className="detail"><section className="section"><h1>HSK</h1><div className="level-grid">
    {data.word_counts.map(row => <button key={row.level} onClick={() => go(`/hsk/${row.level}`)}><b>HSK {row.level}</b><span>{row.count} words</span></button>)}
  </div></section></main>;
  return <main className="detail"><section className="section"><h1>HSK {level}</h1>
    <h2>Words</h2><div className="word-grid">{data.words.map(w => <button key={w.word} onClick={() => go(`/entry/${encodeURIComponent(w.simplified || w.word)}`)}><b>{w.traditional || w.word}</b><span>{w.simplified}</span></button>)}</div>
    <h2>Characters</h2><div className="char-grid">{data.characters.map(c => <button key={c.char} onClick={() => go(`/entry/${encodeURIComponent(c.char)}`)}>{c.char}</button>)}</div>
  </section></main>;
}

function ReadingPage({ system, reading }) {
  const [data, setData] = useState(null);
  useEffect(() => { api(`/api/readings/${system}/${encodeURIComponent(reading)}`).then(setData); }, [system, reading]);
  if (!data) return <main className="panel">Loading...</main>;
  return <main className="detail"><section className="section"><h1>{system}: {reading}</h1>
    <div className="char-grid">{data.characters.map((c, i) => <button key={i} onClick={() => go(`/entry/${encodeURIComponent(c.char)}`)}>{c.char}</button>)}</div>
    <div className="word-grid">{data.words.map(w => <button key={w.id} onClick={() => go(`/entry/${encodeURIComponent(w.simplified || w.traditional)}`)}><b>{w.traditional}</b><span>{asDefs(w.definitions)}</span></button>)}</div>
  </section></main>;
}

function VariantPage({ text }) {
  const [data, setData] = useState(null);
  useEffect(() => { api(`/api/variants/${encodeURIComponent(text)}`).then(setData); }, [text]);
  if (!data) return <main className="panel">Loading...</main>;
  return <main className="detail"><section className="section"><h1>Variants: {text}</h1>
    <h2>Character Variants</h2>
    <div className="table-list">{data.character_variants.map((v, i) => <button className="variant" key={i} onClick={() => go(`/entry/${encodeURIComponent(v.variant)}`)}><b>{v.char}</b><span>{v.variant_type}</span><b>{v.variant}</b><small>{v.source}</small></button>)}</div>
    <h2>Conversion Mappings</h2>
    <div className="table-list">{data.conversion_mappings.map((v, i) => <button className="variant" key={i} onClick={() => go(`/entry/${encodeURIComponent(v.target_text)}`)}><b>{v.source_text}</b><span>{v.dictionary}</span><b>{v.target_text}</b><small>{v.source}</small></button>)}</div>
  </section></main>;
}

function SourcesPage() {
  const [data, setData] = useState(null);
  useEffect(() => { api('/api/sources').then(setData); }, []);
  return <main className="detail"><section className="section"><h1>Sources</h1>{data?.sources?.map(s => <article className="row-card" key={s.key}><b>{s.key}</b><span>{s.license}</span><small>{s.sha256}</small></article>)}</section></main>;
}

function App() {
  const route = useHashRoute();
  const parts = useMemo(() => route.split('/').filter(Boolean), [route]);
  let page = <main className="detail"><section className="section search-home"><SearchBox onResults={() => {}} /></section></main>;
  if (parts[0] === 'entry') page = <EntryPage text={decodeURIComponent(parts.slice(1).join('/'))} />;
  if (parts[0] === 'char' || parts[0] === 'word') page = <EntryPage text={decodeURIComponent(parts.slice(1).join('/'))} />;
  if (parts[0] === 'read') {
    const entryMarker = parts.indexOf('entry');
    const textParts = entryMarker >= 0 ? parts.slice(1, entryMarker) : parts.slice(1);
    const selectedParts = entryMarker >= 0 ? parts.slice(entryMarker + 1) : [];
    page = <ReaderPage initialText={decodeURIComponent(textParts.join('/'))} selectedEntry={decodeURIComponent(selectedParts.join('/') || '')} />;
  }
  if (parts[0] === 'hsk') page = <HskPage level={parts[1]} />;
  if (parts[0] === 'pinyin') page = <ReadingPage system="pinyin" reading={decodeURIComponent(parts[1] || '')} />;
  if (parts[0] === 'jyutping') page = <ReadingPage system="jyutping" reading={decodeURIComponent(parts[1] || '')} />;
  if (parts[0] === 'variants') page = <VariantPage text={decodeURIComponent(parts.slice(1).join('/'))} />;
  if (parts[0] === 'sources') page = <SourcesPage />;
  return <div className="app"><div className="shell">{page}</div></div>;
}

createRoot(document.getElementById('root')).render(<App />);
