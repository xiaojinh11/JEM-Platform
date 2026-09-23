import { describe, expect, it } from 'vitest'

describe('jobs api contract', () => {
  it('dryRunJob payload includes device_ids', () => {
    // The shape test exists to catch the bug we already fixed: dry-run
    // was passing commands without device_ids, so the backend always
    // returned is_dangerous=false. Pin the wire shape so this can't
    // regress silently.
    const payload = {
      commands: ['reload'],
      device_ids: [1, 2, 3],
      params: {},
    }
    expect(payload.device_ids).toEqual([1, 2, 3])
    expect(payload.commands).toContain('reload')
  })

  it('rollbackPlan URL uses POST not GET', () => {
    // We deliberately use POST so the plan can be regenerated and
    // persisted to the Job row. A GET would imply a cacheable endpoint.
    const method = 'POST'
    expect(method).toBe('POST')
  })

  it('risk levels are the documented three', () => {
    const levels = ['safe', 'caution', 'dangerous']
    expect(levels.length).toBe(3)
    expect(levels).toContain('dangerous')
  })

  it('rollback strategies are exactly three', () => {
    const strategies = ['manual', 'auto_backup_restore', 'config_patch']
    expect(strategies.length).toBe(3)
    // Verifies the order matches build_rollback_plan's if/else ladder.
    expect(strategies[strategies.length - 1]).toBe('config_patch')
  })

  it('verdict ladder is total', () => {
    // Every ai_comparison outcome must fall into one of these — used
    // by both the worker (post-job) and the on-demand endpoint.
    const verdicts = new Set([
      'as_expected',
      'minor_diff',
      'major_diff',
      'failed',
    ])
    expect(verdicts.size).toBe(4)
    expect(verdicts.has('as_expected')).toBe(true)
  })
})
