// BDD suite: features/**/*.feature, steps and support in TypeScript (loaded through tsx).
export default {
  paths: ['features/**/*.feature'],
  import: ['features/support/**/*.ts', 'features/step_definitions/**/*.ts'],
  format: ['progress', 'summary', 'html:cucumber-report.html'],
  formatOptions: { snippetInterface: 'async-await' },
};
