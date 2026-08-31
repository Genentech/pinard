import { defineTask } from '@a5c-ai/babysitter-sdk';

const step1 = defineTask('step-1', (args) => ({
  kind: 'agent',
  title: 'Step 1: Count to 5',
  agent: {
    name: 'counter',
    prompt: {
      role: 'A helpful assistant',
      task: 'Count from 1 to 5, one number per line.',
      outputFormat: 'A JSON object with the numbers array',
    },
    outputSchema: {
      type: 'object',
      properties: { numbers: { type: 'array', items: { type: 'number' } } },
      required: ['numbers'],
    },
  },
}));

const step2 = defineTask('step-2', (args) => ({
  kind: 'agent',
  title: 'Step 2: Sum the numbers',
  agent: {
    name: 'summer',
    prompt: {
      role: 'A helpful assistant',
      task: `Sum these numbers: ${JSON.stringify(args.numbers)}. Return the total.`,
      outputFormat: 'A JSON object with the sum',
    },
    outputSchema: {
      type: 'object',
      properties: { sum: { type: 'number' } },
      required: ['sum'],
    },
  },
}));

const step3 = defineTask('step-3', (args) => ({
  kind: 'agent',
  title: 'Step 3: Is the sum even or odd?',
  agent: {
    name: 'checker',
    prompt: {
      role: 'A helpful assistant',
      task: `Is ${args.sum} even or odd? Answer with the parity.`,
      outputFormat: 'A JSON object with parity field',
    },
    outputSchema: {
      type: 'object',
      properties: { parity: { type: 'string', enum: ['even', 'odd'] } },
      required: ['parity'],
    },
  },
}));

export async function process(inputs = {}, ctx) {
  const r1 = await ctx.task(step1, {});
  const r2 = await ctx.task(step2, { numbers: r1.numbers });
  const r3 = await ctx.task(step3, { sum: r2.sum });
  return { numbers: r1.numbers, sum: r2.sum, parity: r3.parity };
}
