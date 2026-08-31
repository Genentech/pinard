import { defineTask } from '@a5c-ai/babysitter-sdk';

const greetTask = defineTask('greet', (args) => ({
  kind: 'agent',
  title: 'Say hello',
  agent: {
    name: 'greeter',
    prompt: {
      role: 'A friendly assistant',
      task: `Say hello to ${args.name || 'world'}. Respond with a short greeting.`,
      outputFormat: 'A single greeting message',
    },
    outputSchema: {
      type: 'object',
      properties: {
        greeting: { type: 'string' },
      },
      required: ['greeting'],
    },
  },
}));

export async function process(inputs = {}, ctx) {
  const result = await ctx.task(greetTask, { name: inputs?.name || 'world' });
  return { greeting: result.greeting, status: 'completed' };
}
