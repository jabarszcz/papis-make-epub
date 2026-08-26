// Math-to-SVG co-process for MathjaxRenderer in math_renderers.py
const mj = require('mathjax-node');
const readline = require('readline');
mj.config({ displayErrors: false });
mj.start();
process.stdout.write('ready\n');
const rl = readline.createInterface({ input: process.stdin, terminal: false });
let chain = Promise.resolve();
rl.on('line', line => {
  const req = JSON.parse(line);
  chain = chain.then(() =>
    mj.typeset({ math: req.math, format: req.format, svg: true })
      .then(r => r.svg || null)
      .catch(() => null)
      .then(svg => { process.stdout.write(JSON.stringify(svg) + '\n'); })
  );
});
