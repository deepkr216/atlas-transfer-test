# V14-listing-moved-twin - a listing moved to SHARED\LISTINGS while another system holds a program of its name forces no re-parse

Found by the verifier of ROADMAP re-parse item 21, confirmed by the acceptance test (s_listing_moved, s_listing_gone). KVTPGM sits in KVA and in KVB, each system with its own DUPREC. KVA's current listing names KVB's library, and the build follows it (recover, build). Then the listing is moved to SHARED\LISTINGS: with a program of the name in KVB, only the listings in KVA speak for KVA's program (item 19), so none does, and a full parse gives KVA's own copy. Tool: KVB's copy kept - no member KVTPGM copies moved, so nothing was parsed again - and recover said 'unknown'.

Fixed in LESSONS 249: each build asks the resolver again for the choices among several of the programs it keeps (`build.picks_moved`) - the system of the listing each row names is read at every build - and parses again those it would now make differently.
