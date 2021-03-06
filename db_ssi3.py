import collections, random, sys, traceback
from dbsim import *

class EagerFailWrite(AbortTransaction): pass
class EagerFailWriteDep(AbortTransaction): pass
class EagerFailReadDep(AbortTransaction): pass
class EagerFailReadAntiDep(AbortTransaction): pass
class EarlyPrecommitFail(AbortTransaction): pass
class PrecommitFail(AbortTransaction): pass

class ReadSkew(AbortTransaction):
    '''Read and overwrote different versions'''
    pass

class WriteSkew(AbortTransaction):
    '''Overwrote a version created after my snapshot'''
    
def make_db(tracker, stats, nrec, tid_watch, rid_watch, read_only_opt,
            version_reader,
            si_relax_writes, si_relax_reads, verbose, **extra_kwargs):
    '''For purposes of Test #1, which examines T1 x:x T r:w T2, we mostly
    have to worry about anti-dependency edges of the form (TA r:w TB):
    
    • If TB commits < TA reads, then TA is aware of TB, but chooses
      not to "see" the write for some reason.

	o TA updates s(TA) with s(TB) at the time of the read.
    
	o TB does nothing, TA arrives too late to affect p(TB).
    
    • If TB overwrites < TA reads < TB commits, then TA is aware of TB
      but is not not allowed to "see" the write. 

	o TA updates s(TA) with s(TB) at pre-commit iff TB has
	  committed by then.

	o Either TA will set t(V)=TA at read (allowing TB to update
	  p(TB) at pre-commit if TA has committed), or TA will adjust
	  p(TB) directly at pre-commit.

    • If TA reads < TB overwrites < TA commits, then TA is *not* aware
      of TB at read time and will have to verify its read set during
      pre-commit unless it gets some help from TB.
    
    	o If t(V) is inactive, TA sets t(V)=TA
    
    	o If t(V) is active, TA updates s(TA) at pre-commit iff TB has
    	  already committed.

    	o TB updates s(TA) at pre-commit iff TA has not yet committed.
    
    	o TB updates r(V) at pre-commit if t(V) is active and
    	  committed. TB also updates p(TB) using r(V) at pre-commit.

    	o Note that TA must still follow this protocol if it sees an
          uncommitted overwrite, because that overwrite could abort
          and some later write could still arrive in time to matter.
    
    • If TA commits < TB overwrites, then p(TB) must account for
      TA. However, we only care about the latest such TA, because
      p(TB) is a maximum.
    
    	o TB does not impact s(TA). TA must make c(TA) available to TB
    	  either by setting t(V) (at read) or r(V) (at pre-commit).

    	o TB updates p(TB) with both r(V) and c(t(V)), as appropriate,
    	  at write time.

    '''
    q,e = not verbose, errlog

    '''One gotcha: under SSN+SI, the read-only opt only helps writers who
    happen to succeed a reader. Worse, those very same writers tend to
    cause other read-only transactions to abort, causing a net lower
    completion rate. This is a major disadvantage compared to SSI.

    A minimal example might be (with commit times in parentheses):

    (1)    (3)   (5)    (2)
    T0 w:r Ta
              r:w
                  T r:w Tb
              r:w
    Tx w:r Tr
    (4)    (6)

    If we apply the read-only optimization to Ta r:w T, then T will
    not abort like it normally would. However, allowing T to commit
    causes read-only Tr to abort, because \pi(Tr)=2 <= c(Tx)=4 <=
    c(Tr)=6. Tr's predecessor is guaranteed to be a writer, and so the
    read-only optimization cannot save Tr; indeed, Tr could read Tb's
    write, which would close the cycle Tr r:w T r:w Tb w:r Tr.

    As far as I can tell, this same issue should impact SSI, though:
    Tb committed first and \sigma(Tr) does not precede c(Tb). That
    leaves the question of (a) why a saved writer kills multiple
    readers and (b) why SSI doesn't suffer as badly in practice.

    '''
    # interaction: read-only optimization only works under SI
    read_only_opt &= not si_relax_reads
    
    stats.read_forget,stats.read_revisit = 0,0
    
    tx_sstamps = {1:1}
    tx_pstamps = collections.defaultdict(lambda:0)
    tx_reads = collections.defaultdict(dict)
    tx_writes = collections.defaultdict(set)

    '''Each version V is associated with several pieces of information:

    - TID of its creator (the tracker manages this for us). Note that
      the TID may belong to an "old" transaction that the system has
      forgotten, in which case we won't be able to extract any further
      information about it.

    - Creation time. The tracker manages this also. Crucially, the
      timestamp remains in the version even after the transaction that
      creates it has been forgotten.

    - Read stamp. The most recent commit stamp of any transaction that
      read this version. Every reader must update this stamp on any
      version that has not been overwritten before the read
      commits. The read stamp is used to update p(T) of the
      transaction that eventually overwrites this version.
    
    - Active reader. Each version can store the TID of one transaction
      that is currently reading it. A transaction does not have to
      update the read stamp if it can leave its TID instead, reducing
      the amount of work it performs at pre-commit. Any reader or
      overwrite that comes later will update the read stamp with the
      active reader's commit stamp. If an active reader is forgotten
      before another transaction accesses the version, its commit time
      is not available; in these cases, the current safe point is used
      instead. This may cause some extra false positives in cycle
      detection, but not many (it would require a chain of
      anti-dependencies to span several epochs. If the loss of
      precision is unacceptable, the active reader slot should be
      eliminated, forcing all readers to set read stamps.
    
    - NOTE: We do *NOT* track s(T) of V's creator. T will not be
      forgotten until V dies, so any TR that is able to access V can
      still look up s(T).

    '''
    v_readers = collections.defaultdict(lambda:0)
    v_rstamps = collections.defaultdict(lambda:0)
    v_sstamps = {}

    def get_version_reader(rid, dep):
        reader = v_readers.get((rid,dep), 0)
        if not version_reader:
            return reader,None
        rc = tracker.get_safe_point()
        if tracker.is_known(reader):
            commit = tracker.is_committed(reader)
            if commit:
                # read-only optimization: use begin time of committed
                # read-only reader
                if read_only_opt and reader not in tx_writes:
                    commit = tracker.get_begin(reader)
            elif commit is False:
                reader = 0 # aborted!
                
            rc = commit

        return reader,rc
        
    def tx_read(pid, rid, for_update=False):
        Q = q and pid not in tid_watch and rid not in rid_watch

        y,dprev,clobber = None,None,None
        pt,st = tx_pstamps[pid],tx_sstamps.get(pid, None)
        def read_filter(it):
            # take the newest version that precedes our snapshot.
            nonlocal clobber
            start = tracker.get_begin(pid)
            for x,dep,stamp in it:
                if si_relax_reads > 1 or pid == dep or stamp < start:
                    if si_relax_reads == 1 and pid != dep and clobber:
                        # check for fatal anti-dep
                        assert pid != dep
                        assert tracker.is_known(clobber)
                        sxmax = tx_sstamps.get(clobber, None)
                        if sxmax and not (pt < clobber):
                            clobber=dprev
                            return y
                        if st and not (stamp < st):
                            clobber=dprev
                            return y
                    return x

                y,dprev,clobber = x,clobber,dep

            errlog('Oops! T=%d finds no suitable version of R=%d', pid, rid)
            errlog('\t(last version was %d, visible since %s, T started at %d)',
                   dep, stamp, start)
            assert not 'reachable'
                    
        dep,cdep = yield from tracker.on_access(pid, rid, read_filter)

        Q or e('read: pid=%s rid=%s', pid, rid)

        # deal with inbound dependency edge
        Q or e('\tInbound dependency: T=%d p(T)=%d X=%d c(X)=%s',
               pid, pt, dep, cdep)
        if cdep > pt:
            pt = tx_pstamps[pid] = cdep
        if st and not (pt < st):
            raise EagerFailReadDep
               
        # check for outbound anti-dependency edge. Note that sc can be
        # missing even if there was a clobber, but no clobber => no sc
        sc = v_sstamps.get((rid,dep), None)
        assert clobber or not sc
        if sc:
            # reading under a committed overwrite
            Q or e('\tOutbound anti-dependency: T=%d s(T)=%s X=%d s(X)=%d',
                   pid, st, clobber, sc)
            if not st or sc < st:
                st = tx_sstamps[pid] = sc
                
            if not (pt < st):
                raise EagerFailReadAntiDep
                
        elif not clobber:
            # have to worry about a future overwrite
            rstamp = v_rstamps[rid,dep]
            reader,rc = get_version_reader(rid,dep)
            if not rc:
                # have to revisit during pre-commit
                Q or e('\tT=%d adds version to read footprint', pid)
                if dep != pid:
                    x = tx_reads[pid].setdefault(rid, dep)
                    if x != dep:
                        assert x != pid
                        # oops, already read a different version, and
                        # neither one was created by this transaction
                        raise ReadSkew
                stats.read_revisit += tracker.is_tracked(pid)
                
            else:
                # update rstamp as necessary and claim the slot
                if rstamp < rc:
                    rstamp = v_rstamps[rid,dep] = rc
                    
                Q or e('\tT=%d marks version', pid)
                reader = v_readers[rid,dep] = pid
                stats.read_forget += tracker.is_tracked(pid)

        yield from sys_busy(random.randint(ONE_TICK, 2*ONE_TICK),
                            color='green', title='%s=db[%s]' % (dep, rid))
        return dep
        

    def tx_write(pid, rid):
        Q = q and pid not in tid_watch and rid not in rid_watch
        Q or e('write: pid=%s rid=%d', pid, rid)

        # do the write
        dep,cdep = yield from tracker.on_access(pid, rid, False)
        
        # have I written this before?
        if dep == pid:
            q or e('\talready wrote to this record')
            return

        # Did we read a different version than this? We can't know for
        # sure, because we don't actually track the full read set, but
        # we can at least check the reads we did record.
        rdep = tx_reads.get(pid, {}).get(rid, None)
        if not si_relax_writes and rdep and rdep != dep:
            raise ReadSkew
        
        # can I actually see the version I'm overwriting?
        if not si_relax_writes and cdep > tracker.get_begin(pid):
            raise WriteSkew
        
        # record the write
        tx_writes[pid].add((rid,dep))
        
        # deal with inbound dependency edge
        pt,st = tx_pstamps[pid],tx_sstamps.get(pid,None)
        Q or e('\tInbound dependency: T=%d p(T)=%d X=%d c(X)=%d',
               pid, pt, dep, cdep)
        if pt < cdep:
            pt = tx_pstamps[pid] = cdep
        if st and not (pt < st):
            raise EagerFailWriteDep

        # check for inbound anti-dependency edges
        rstamp = v_rstamps[rid,dep]
        reader,rc = get_version_reader(rid,dep)
        if reader == pid:
            Q or e('\tI previously read this version!')
            reader = v_readers[rid,dep] = 0
        elif rc and rstamp < rc:
            Q or e('\tT=%d found committed read mark: r(V)=%s X=%d c(X)=%s',
                   pid, rstamp, reader, rc)
            rstamp = v_rstamps[rid,dep] = rc

        Q or e('\tDeal with committed inbound anti-deps: T=%d p(T)=%d r(V)=%d',
               pid, pt, rstamp)
        if pt < rstamp:
            pt = tx_pstamps[pid] = rstamp
            if st and not (pt < st):
                q or e('EagerFailWrite pid=%d', pid)
                raise EagerFailWrite

        # no outbound edges possible
        yield from sys_busy(random.randint(ONE_TICK, 2*ONE_TICK),
                            color='blue', title='%s=db[%s]' % (dep, rid))
        
    tid_watched = set()
    def tx_create(pid, is_readonly):
        if tid_watch and len(tid_watch) == len(tid_watched):
            hanging = set()
            for x in tid_watched:
                if tracker.is_known(x):
                    hanging.add(x)
            if hanging:
                errlog('Unfinalized pids: %s', ','.join(map(str, hanging)))
            yield from sys_exit(0)
            
        #t.begin = yield from sys_now()
        tracker.on_begin(pid)

    def tx_commit(pid):
        Q = q and pid not in tid_watch
        yield from sys_busy(random.randint(ONE_TICK, 2*ONE_TICK), color='yellow')
        Q or e('Commit %s', pid)
        if pid in tid_watch:
            tid_watched.add(pid)

        # get a serialization point, not reused even if we abort
        end = tracker.on_precommit(pid)
        pt,st = tx_pstamps[pid],tx_sstamps.setdefault(pid, end)
        if st == end:
            Q or e('\tT=%d initialized s(T)=%s at pre-commit', pid, end)
        else:
            Q or e('\tT=%d has s(T)=%s at start of pre-commit', pid, end)
        
        # /// BEGIN CRITICAL SECTION ///

        # read-only optimization: I have to update rstamps of all
        # remembered reads, but if I finished read-only then I can use
        # my begin timestamp instead of my end timestamp, thus
        # reducing the probability that I violate some overwriter's
        # exclusion window. In case of forgotten reads, they will get
        # the right value by calling get_version_reader().
        ct = end
        if read_only_opt and pid not in tx_writes:
            ct = tracker.get_begin(pid)
        
        # check for reads that commit under an overwrite
        safe_point = tracker.get_safe_point()
        for rid,dep in tx_reads.get(pid, {}).items():
            rstamp = v_rstamps[rid,dep]
            Q or e('\tT=%d verifying read of R%d/T%d', pid, rid, dep)

            # skip versions we overwrote
            if (rid,dep) in tx_writes.get(pid, ()):
                continue

            # Check for overwrites. If no clobber has shown up yet,
            # pretend this transaction is the overwriter (guaranteed
            # to be reported as in-flight) to force an rstamp update.
            x,_ = tracker.get_overwriter(rid, dep)
            if not x:
                # no committed overwrite, have to update rstamp
                Q or e('\tUpdate r(V): T=%d c(T)=%d r(V)=%d',
                       pid, ct, rstamp)
                if rstamp < ct:
                    rstamp = v_rstamps[rid,dep] = ct

            else:
                # overwrite committed after we read, so update s(T)
                sx = v_sstamps.get((rid,dep), None)
                Q or e('\tCommitted clobber: T=%d s(T)=%d X=%d s(X)=%d',
                       pid, st, x, sx)
                if sx and st > sx:
                    st = tx_sstamps[pid] = sx

        for rid,dep in tx_writes.get(pid,()):
            Q or e('\tT=%d verifying overwrite of R%d/T%d', pid, rid, dep)
            rstamp = v_rstamps[rid,dep]
            reader,rc = get_version_reader(rid,dep)
            if not rc:
                sr = tx_sstamps.get(reader,None)
                Q or e('\tUpdate s(X) for in-flight reader: T=%d s(T)=%s X=%d s(X)=%s',
                       pid, st, reader, sr)
                if tracker.is_known(reader) and (not sr or sr > st):
                    sr = tx_sstamps[reader] = st
            else:
                Q or e('\tActive reader committed: V=%d, X=%d c(X)=%d r(V)=%d',
                       rid, reader, rc, rstamp)
                if rstamp < rc:
                    rstamp = v_rstamps[rid,dep] = rc

            Q or e('\tAccount for committed readers: T=%d p(T)=%d r(V)=%d',
                   pid, pt, rstamp)
            if pt < rstamp:
                pt = tx_pstamps[pid] = rstamp

        if not (pt < st):
            Q or e('\tPrecommitFail pid=%d', pid)
            raise PrecommitFail

        # update version stamps now that we've committed
        for rid,dep in tx_writes.get(pid,()):
            v_sstamps[rid,dep] = st
            
        Q or e('\tT=%d at commit: p(T)=%d s(T)=%d', pid, pt, st)
        # /// END CRITICAL SECTION ///

        tx_reads.pop(pid,None)
        tx_pstamps.pop(pid,None)
        tx_sstamps.pop(pid,None)
        yield from tracker.on_finish(pid, True, finish_callback)
        yield from sys_sleep(random.randint(5*ONE_TICK, 10*ONE_TICK))
        yield from sys_busy(random.randint(ONE_TICK, 2*ONE_TICK), color='orange')
        
    def finish_callback(pid):
        # don't need the actual writes... just whether they wrote
        tx_writes.pop(pid,None)
        
    def tx_abort(pid):
        Q = q and pid not in tid_watch
        if pid in tid_watch:
            tid_watched.add(pid)
            
        tx_reads.pop(pid,None)
        tx_pstamps.pop(pid,None)
        tx_sstamps.pop(pid,None)
        Q or e('Abort %d', pid)
        yield from sys_busy(random.randint(ONE_TICK, 2*ONE_TICK), color='red')
        yield from tracker.on_finish(pid, False, finish_callback)


    def fini():
        print_general_stats(stats)
        rcount = .01*(stats.read_revisit+stats.read_forget) or 1
        errlog('    Read forget/revisit: %.1f%%/%.1f%% (%d/%d)',
               stats.read_forget/rcount, stats.read_revisit/rcount,
               stats.read_forget, stats.read_revisit)
        print_failure_causes(stats)


    return NamedTuple(nrec=nrec, tx_begin=tx_create,
                      tx_read=tx_read, tx_write=tx_write,
                      tx_commit=tx_commit, tx_abort=tx_abort,
                      fini=fini,
                      begin_tracking=tracker.begin_tracking,
                      end_tracking=tracker.end_tracking)
        
                


def test_ssi3_db():

    R,U,X = 1,2,3
    def test_fini(db):
        done = False
        def callback():
            nonlocal done
            done = True

        def nop():
            pid = yield from sys_getpid()
            yield from db.tx_begin(pid)
            yield from db.tx_abort(pid)

        yield from sys_sleep(1000*ONE_TICK)
        db.end_tracking(callback)
        yield from sys_spawn(nop())
        yield from sys_sleep(1000*ONE_TICK)
        yield from sys_spawn(nop())
        yield from sys_sleep(10000*ONE_TICK)
        assert done    
        db.fini()
        yield from sys_exit()
            

    def access(db, pid, rid, mode, delay):
        yield from sys_sleep(delay*ONE_TICK)
        yield from db.tx_write(pid, rid) if mode == X else db.tx_read(pid, rid, mode == U)

    def commit(db, pid, delay):
        if not isinstance(delay, int):
            errlog('bad delay: %s', delay)
            
        yield from sys_sleep(delay*ONE_TICK)
        yield from db.tx_commit(pid)
        
    def tx_one(db, rid, mode, delay1, delay2):
        def thunk():
            pid = yield from sys_getpid()
            try:
                yield from db.tx_begin(pid)
                yield from access(db, pid, rid, mode, delay1)
                yield from commit(db, pid, delay2)
            except AbortTransaction:
                yield from db.tx_abort(pid)
                
        return (yield from sys_spawn(thunk()))
        
    def tx_two(db, rid1, mode1, rid2=None, mode2=None, delay1=0, delay2=0, delay3=0):
        def thunk():
            pid = yield from sys_getpid()
            try:
                yield from db.tx_begin(pid)
                yield from access(db, pid, rid1, mode1, delay1)
                yield from access(db, pid, rid2 or rid1, mode2 or mode1, delay2)
                yield from commit(db, pid, delay3)
            except AbortTransaction:
                yield from db.tx_abort(pid)
                
        return (yield from sys_spawn(thunk()))

    def tx_n(db, commit_delay, *args):
        # accept (rid,mode,delay) triples
        def thunk():
            pid = yield from sys_getpid()
            try:
                yield from db.tx_begin(pid)
                for rid,mode,delay in args:
                    yield from access(db, pid, rid, mode, delay)
                yield from commit(db, pid, commit_delay)
            except AbortTransaction:
                yield from db.tx_abort(pid)
                
        return (yield from sys_spawn(thunk()))
        
    def test1(db):
        '''reads coexist peacefully'''
        # R-lock at t=0, S-locks at t=2 and t=2
        yield from tx_one(db, 1, R, 0, 10)
        yield from tx_one(db, 1, R, 1, 8)
        yield from tx_one(db, 1, R, 2, 6)
        yield from test_fini(db)

    def test2(db):
        '''incompatible requests block until the holder leaves'''
        yield from tx_one(db, 1, R, 0, 10)
        yield from tx_one(db, 1, X, 2, 0)
        yield from test_fini(db)
            
            
    def test3(db):
        '''reads can coexist with one upgrade lock, but a second upgrader
        blocks until the first leaves. Also make sure that the first
        upgrader can still upgrade when the lock mode is W.

        '''
        yield from tx_one(db, 1, R, 0, 10)
        yield from tx_two(db, rid1=1, mode1=U, mode2=X, delay1=2, delay2=8)
        yield from tx_two(db, rid1=1, mode1=U, mode2=X, delay1=3, delay2=6)
        yield from test_fini(db)
            

    def test4(db):
        '''new readers can coexist with an upgrade lock but not with an
        in-progress upgrade.

        '''
        yield from tx_one(db, 1, R, 0, 4)
        yield from tx_two(db, rid1=1, mode1=U, mode2=X, delay1=1, delay2=1)
        yield from tx_one(db, 1, R, 2, 4)
        yield from tx_one(db, 1, R, 4, 0)
        yield from test_fini(db)

    def test5(db):
        '''reader cannot upgrade if an upgrade lock has been granted'''
        yield from tx_two(db, rid1=1, mode1=R, mode2=X, delay2=5, delay3=1)
        yield from tx_one(db, 1, U, 1, 10)
        yield from test_fini(db)

    def test6(db):
        '''simple two-party deadlock detected'''
        yield from tx_two(db, rid1=1, mode1=R, rid2=2, mode2=X, delay2=5)
        yield from tx_two(db, rid1=2, mode1=R, rid2=1, mode2=X, delay1=1)
        yield from test_fini(db)

    def test7(db):
        '''three-party deadlock detected'''
        yield from tx_two(db, rid1=1, mode1=R, rid2=2, mode2=X, delay2=5)
        yield from tx_two(db, rid1=2, mode1=R, rid2=3, mode2=X, delay1=1)
        yield from tx_two(db, rid1=3, mode1=R, rid2=1, mode2=X, delay1=2)
        yield from test_fini(db)

    def test8(db):
        '''SI read-only anomaly detected:
	T1		T2		T3
			read A
			read B
					write B' (T2)
					D-commit (T2)
	read A
	read B' (T3)
	read C
	D-commit (T2 T3)
			write C' (T1)
			!! abort !!
					I-commit
	I-commit
        '''
        yield from tx_n(db, 0, (0, R, 200), (1, R, 0), (2, R, 0))
        yield from tx_n(db, 0, (0, R, 0), (1, R, 0), (2, X, 300))
        yield from tx_n(db, 0, (1, X, 100))
        yield from test_fini(db)

    A,B,C,D = 0,1,2,3

    def test9(db):
        '''Complex scenario that SI and 2PL both reject:
	T1		T2		T3		T4		T5
	read A
							write A' (T1)
							D-commit (T1)
					read B
    			write B' (T3)
			write C'
			D-commit (T3)
									write C'' (T2)
									D-commit (T2, T3)    
    	read C'' (T5)
					read C (**)
    					write D'
					DI-commit
			I-commit
									I-commit
	write D''
	DI-commit
							I-commit    
        
        '''
        yield from tx_n(db, 0, (A, R, 0), (C, R, 500), (D, X, 300))
        yield from tx_n(db, 0, (B, X, 300), (C, X, 0))
        yield from tx_n(db, 0, (B, R, 200), (C, R, 400), (D, X, 0))
        yield from tx_n(db, 0, (A, X, 100))
        yield from tx_n(db, 0, (C, X, 400))
        yield from test_fini(db)

    def test10(db):
        '''Schedule found during a measurement run. The Dangerous Structure
        has not yet formed when T2 commits first; when
        T3 commits second it sees T1 rw(A) T2 rw(B) T3:
        
	T1		T2		T3		
	                read B
	                		write B'
	                write A'
	read A          				
	                D-commit
	                                write C'
	                                D-commit
	read C'                         		
	D-commit	                                

        '''
        yield from tx_n(db, 100, (A, R, 0), (C, X, 200))
        yield from tx_n(db, 0,   (B, X, 100), (A, X, 400))
        yield from tx_n(db, 0,   (C, R, 300), (B, R, 300))
        yield from test_fini(db)

    def test11(db):
        '''Schedule found during a measurement run. The Dangerous Structure
        has not yet formed when T2 commits first; when
        T3 commits second it sees T1 rw(A) T2 rw(B) T3:
        
	T1		T2		T3
			read B
			write A'
					write C'
	read A
			commit
					write B'
	                                commit
	write C''
	commit

        '''
        yield from tx_n(db, 0, (A, R, 200), (C, X, 300))
        yield from tx_n(db, 300,   (B, R, 0), (A, X, 0))
        yield from tx_n(db, 0,   (C, X, 200), (B, X, 300))
        yield from test_fini(db)

    def test12(db):
        '''Schedule found during a measurement run. A dependency cycle forms
        without manifesting any Dangerous Structures:
        
	T1		T2		T3		T4
	                				read A
	write A'
	write B'
			read C
	D-commit
					write C'
	                                write D'
	                                D-commit
	                                		read D'
			read B'
	                				D-commit
			D-commit


        Adversarial commit ordering can make it even nastier. Here, T1
        is arguably the problem transaction, since its read of D' is
        impossible under SI, but it commits before T3 even reads
        C'. That means it's not enough for T1 to detect that it has
        gone awry; T3 must know to abort itself even though it has
        done nothing "wrong."
        
	T1		T2		T3		T4		
	read A                          				
	                write A'
	                write C'
	                D-commit
	                		read B
	                				write B'
	                                                write D'
	                                                D-commit
	read D'                                         		
	D-commit                        				
	                		read C'
					D-commit

        Note that there's still a shadow of the Dangerous Structure
        here. Every cycle must include (at least) two RW dependencies:
        one leaks information out of an uncommitted transaction and
        the other circumvents the commit-time dependency tracking that
        normally prevents the leakage from causing isolation
        failures. The RW deps just don't have to be adjacent any more
        after we give up SI.

        Thought: perhaps we can check D-commit times: it's a bad sign
        if I read a version that was clobbered before 1+ of the
        transactions in my dependency set. In the above example, T3
        depends on {T1 T2}, and gives a RW dep to T4; T4 D-committed
        before T1, and so T1 could potentially be poisoned by
        T4. There could be a lot of false positives, though: if T4 did
        not write D' at all (writing only B', for example), then there
        is no cycle. This looks suspiciously similar to wound-wait,
        which has a painfully high false positive rate.

        '''
        yield from tx_n(db, 300, (A, X, 0), (B, X, 0))
        yield from tx_n(db, 200, (C, R, 100), (B, R, 600))
        yield from tx_n(db, 0,   (C, X, 500), (D, X, 0))
        yield from tx_n(db, 200, (A, R, 0), (D, R, 600))
        yield from test_fini(db)

    def test13(db):
        '''One of the simplest possible serialization anomalies
        T1		T2
        read A
        		write A'
        		write B'
        		D-commit
        read B
        D-commit
        '''
        yield from tx_n(db, 0, (A, R, 0), (B, R, 300))
        yield from tx_n(db, 0, (A, X, 100), (B, X, 0))
        yield from test_fini(db)

    def test14(db):
        '''Scenario suggested by Alan Fekete:

	T1		T2		T3
					Write A'
			Read B
					Write B'
	Write C'
					Commit
	Read A'
	Commit
			Write C''
			Commit

        T2 should abort.
        '''
        yield from tx_n(db, 0, (C, X, 400), (A, R, 200))
        yield from tx_n(db, 0, (B, R, 100), (C, X, 600))
        yield from tx_n(db, 200, (A, X, 0), (B, X, 200))
        yield from test_fini(db)

    def test15(db):
        '''This scenario found during simulation run:

        T1		T2		T3		T4
        write A'
        write B'
        		read B
        D-commit
        				read C
        		write C'
        		D-commit
        						read D
        				write D'
        						read A'
        				D-commit
        						D-commit

        Produces the cycle T1 -- T4 rw T3 rw T2 rw T1 
        '''
        yield from tx_n(db, 200, (A, X, 0), (B, X, 0))
        yield from tx_n(db, 0, (B, R, 200), (C, X, 300))
        yield from tx_n(db, 200, (C, R, 400), (D, X, 400))
        yield from tx_n(db, 200, (D, R, 700), (A, R, 200))
        yield from test_fini(db)

    def test16(db):
        '''Scenario found during a simulation run. Requires Test #2 to avoid
        wedging the DB, even under SI. T4 should abort.

        T1		T2		T3		T4
        				read C
        				write D'
        read A
        write C'
        D-commit
        		write A'
        		write B
        		D-commit
        						read B'
        						read D
        				D-commit
        						D-commit

        T1 rw T2 -- T4 rw T3 rw T1

        '''
        yield from tx_n(db, 0, (A, R, 100), (C, X, 0))
        yield from tx_n(db, 0, (A, X, 400), (B, X, 0))
        yield from tx_n(db, 600, (C, R, 0), (D, X, 0))
        yield from tx_n(db, 200, (B, R, 600), (D, R, 0))
        yield from test_fini(db)

    failures = 0
    for test in (test1,test2,test3,test4,test5,test6,test7,test8,test9,
                 test10,test11,test12,test13,test14,test15,test16):
        errlog('\n%s\nRunning test:\n\n\t%s\n', '='*72, test.__doc__)
        stats = NamedTuple()
        tracker = dependency_tracker(stats)
        db = make_ssi3_db(stats=stats, tracker=tracker,nrec=10, ww_blocks=True, verbose=True)
        db.begin_tracking()
        try:
            simulator(test(db), log=log_svg_task())
        except:
            traceback.print_exc()
            failures +=1
        
    if failures:
        exit(1)
    
        
        
    
    
if __name__ == '__main__':
    try:
        seed = sys.argv[1]
    except:
        seed = make_seed()

    errlog('Using RNG seed: %s', seed)
    random.seed(seed)

    test_ssi3_db()
    #simulator(make_benchmark(make_db=make_si_db, nclients=50, max_inflight=100, nrec=1000, duration=10000), log=log_svg_task())
