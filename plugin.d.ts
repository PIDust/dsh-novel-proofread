/**
 * Type declarations for dsh-novel-proofread plugin.
 * Pure local novel proofreading skill for DeepSeek Harness.
 */

export declare const name: 'dsh-novel-proofread'
export declare const version: '1.0.0'

export declare function apply(ctx: unknown): void

export interface NovelProofreadSkillDef {
  id: 'novel-proofread'
  name: '小说校对'
  type: 'python-skill'
  entry: string
  main: 'proofread.py'
  description: string
  commands: string[]
  requires: string[]
}

export interface NovelProofreadContext {
  novelProofreadSkillDir?: string
}
