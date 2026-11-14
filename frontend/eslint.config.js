import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import globals from 'globals';
export default tseslint.config({ ignores: ['dist/**', 'storybook-static/**', 'src/api.generated.ts', 'node_modules/**', 'artifacts/**'] }, js.configs.recommended, ...tseslint.configs.recommended, { languageOptions: { globals: { ...globals.browser, ...globals.node } } });
