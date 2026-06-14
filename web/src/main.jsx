import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Volume2 } from 'lucide-react';
import './styles.css';

const API = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000';
const SEARCH_PLACEHOLDER = '天天中彩票';
const READING_LABELS = { pinyin: 'Pinyin', zhuyin: 'Zhuyin', jyutping: 'Jyutping', korean: 'Korean', japanese: 'Japanese' };

async function api(path, options) {
  const res = await fetch(`${API}${path}`, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
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
    api(`/api/entry/${encodeURIComponent(text)}`).then(setData).catch(e => setErr(e.message));
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
  useEffect(() => { setText(initialText); }, [initialText]);
  useEffect(() => {
    const handle = setTimeout(() => {
      api('/api/segment', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})}).then(setData);
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
  function updateText(value) {
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
    <section className="section reader-main">
      <textarea value={text} onChange={e => updateText(e.target.value)} />
      <div className="tokens">{lines.map((line, lineIndex) => {
        const lineText = line.map((token) => token.text).join('');
        return <div className="token-line" key={lineIndex}>
          {line.map((t, i) => isChineseToken(t.text) ? <button key={`${lineIndex}-${i}-${t.start}`} onClick={() => selectEntry(t.text)}><RubyText text={t.text} reading={t.entry?.primary_pinyin || ''} mode={t.text.length > 1 ? 'chars' : 'word'} />{!t.entry?.primary_pinyin && <small>{t.entry?.english_summary}</small>}</button> : <span className="plain-token" key={`${lineIndex}-${i}-${t.start}`}>{t.text}</span>)}
          {!!lineText && <button className="line-speak" aria-label="Play line" onClick={() => speakChinese(lineText)}><Volume2 size={18} /></button>}
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
