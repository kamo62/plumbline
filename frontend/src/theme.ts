export const tokenNames = ['background', 'surface', 'text', 'muted', 'border', 'accent', 'accentText', 'focus'] as const;
export type Tokens = Record<typeof tokenNames[number], string>;
export type Theme = { name: string; tokens: Tokens };
export const themes: Record<'light' | 'dark' | 'highContrast', Theme> = {
  light: { name: 'Light', tokens: { background: '#f3f4f6', surface: '#ffffff', text: '#1c2430', muted: '#535e6d', border: '#697586', accent: '#2457a5', accentText: '#ffffff', focus: '#174386' } },
  dark: { name: 'Dark', tokens: { background: '#171a20', surface: '#22262e', text: '#f1f3f5', muted: '#c2c9d3', border: '#9ba6b5', accent: '#a7c8ff', accentText: '#10213d', focus: '#a7c8ff' } },
  highContrast: { name: 'High contrast', tokens: { background: '#000000', surface: '#000000', text: '#ffffff', muted: '#ffffff', border: '#ffffff', accent: '#ffff00', accentText: '#000000', focus: '#ffff00' } },
};
const luminance = (hex: string) => {
  const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
};
export function contrast(a: string, b: string) { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
export function validateTheme(value: unknown): Theme {
  if (!value || typeof value !== 'object') throw new Error('Theme must be an object.');
  const item = value as Record<string, unknown>;
  if (Object.keys(item).sort().join(',') !== 'name,tokens' || typeof item.name !== 'string' || !item.name.trim() || item.name.length > 80 || !item.tokens || typeof item.tokens !== 'object' || Array.isArray(item.tokens)) throw new Error('Theme needs a name and a tokens object.');
  const tokens = item.tokens as Record<string, unknown>;
  if (Object.keys(tokens).sort().join(',') !== [...tokenNames].sort().join(',') || tokenNames.some(k => typeof tokens[k] !== 'string' || !/^#[0-9a-f]{6}$/i.test(tokens[k] as string))) throw new Error('Provide exactly the required six-digit hex colour tokens.');
  const t = tokens as Tokens;
  for (const [a,b,min] of [['text','surface',4.5],['text','background',4.5],['muted','surface',4.5],['muted','background',4.5],['accentText','accent',4.5],['accent','surface',4.5],['accent','background',4.5],['border','surface',3],['focus','surface',3],['focus','background',3]] as const) {
    if (contrast(t[a], t[b]) < min) throw new Error(`${a} and ${b} need a contrast ratio of at least ${min}:1.`);
  }
  return { name: item.name, tokens: t };
}
export function applyTheme(theme: Theme) { for (const key of tokenNames) document.documentElement.style.setProperty(`--${key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}`, theme.tokens[key]); }
