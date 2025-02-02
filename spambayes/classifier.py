#! /usr/bin/env python



# An implementation of a Bayes-like spam classifier.
#
# Paul Graham's original description:
#
#     http://www.paulgraham.com/spam.html
#
# A highly fiddled version of that can be retrieved from our CVS repository,
# via tag Last-Graham.  This made many demonstrated improvements in error
# rates over Paul's original description.
#
# This code implements Gary Robinson's suggestions, the core of which are
# well explained on his webpage:
#
#    http://radio.weblogs.com/0101454/stories/2002/09/16/spamDetection.html
#
# This is theoretically cleaner, and in testing has performed at least as
# well as our highly tuned Graham scheme did, often slightly better, and
# sometimes much better.  It also has "a middle ground", which people like:
# the scores under Paul's scheme were almost always very near 0 or very near
# 1, whether or not the classification was correct.  The false positives
# and false negatives under Gary's basic scheme (use_gary_combining) generally
# score in a narrow range around the corpus's best spam_cutoff value.
# However, it doesn't appear possible to guess the best spam_cutoff value in
# advance, and it's touchy.
#
# The last version of the Gary-combining scheme can be retrieved from our
# CVS repository via tag Last-Gary.
#
# The chi-combining scheme used by default here gets closer to the theoretical
# basis of Gary's combining scheme, and does give extreme scores, but also
# has a very useful middle ground (small # of msgs spread across a large range
# of scores, and good cutoff values aren't touchy).
#
# This implementation is due to Tim Peters et alia.

import math

# XXX At time of writing, these are only necessary for the
# XXX experimental url retrieving/slurping code.  If that
# XXX gets ripped out, either rip these out, or run
# XXX PyChecker over the code.
import re
import os
import sys
import socket
import urllib.request, urllib.error, urllib.parse
from email import message_from_string

DOMAIN_AND_PORT_RE = re.compile(r"([^:/\\]+)(:([\d]+))?")
HTTP_ERROR_RE = re.compile(r"HTTP Error ([\d]+)")
URL_KEY_RE = re.compile(r"[\W]")
# XXX ---- ends ----

from spambayes.Options import options
from spambayes.chi2 import chi2Q
from spambayes.safepickle import pickle_read, pickle_write

LN2 = math.log(2)       # used frequently by chi-combining

slurp_wordstream = None

PICKLE_VERSION = 5

class WordInfo(object):
    # A WordInfo is created for each distinct word.  spamcount is the
    # number of trained spam msgs in which the word appears, and hamcount
    # the number of trained ham msgs.
    #
    # Invariant:  For use in a classifier database, at least one of
    # spamcount and hamcount must be non-zero.
    #
    # Important:  This is a tiny object.  Use of __slots__ is essential
    # to conserve memory.
    __slots__ = 'spamcount', 'hamcount'

    def __init__(self):
        self.__setstate__((0, 0))

    def __repr__(self):
        return "WordInfo" + repr((self.spamcount, self.hamcount))

    def __getstate__(self):
        return self.spamcount, self.hamcount

    def __setstate__(self, t):
        self.spamcount, self.hamcount = t


class Classifier:
    # Defining __slots__ here made Jeremy's life needlessly difficult when
    # trying to hook this all up to ZODB as a persistent object.  There's
    # no space benefit worth getting from slots in this class; slots were
    # used solely to help catch errors earlier, when this code was changing
    # rapidly.

    #__slots__ = ('wordinfo',  # map word to WordInfo record
    #             'nspam',     # number of spam messages learn() has seen
    #             'nham',      # number of non-spam messages learn() has seen
    #            )

    # allow a subclass to use a different class for WordInfo
    WordInfoClass = WordInfo

    def __init__(self):
        self.wordinfo = {}
        self.probcache = {}
        self.nspam = self.nham = 0

    def __getstate__(self):
        return (PICKLE_VERSION, self.wordinfo, self.nspam, self.nham)

    def __setstate__(self, t):
        if t[0] != PICKLE_VERSION:
            raise ValueError("Can't unpickle -- version %s unknown" % t[0])
        (self.wordinfo, self.nspam, self.nham) = t[1:]
        self.probcache = {}

    # spamprob() implementations.  One of the following is aliased to
    # spamprob, depending on option settings.
    # Currently only chi-squared is available, but maybe there will be
    # an alternative again someday.

    # Across vectors of length n, containing random uniformly-distributed
    # probabilities, -2*sum(ln(p_i)) follows the chi-squared distribution
    # with 2*n degrees of freedom.  This has been proven (in some
    # appropriate sense) to be the most sensitive possible test for
    # rejecting the hypothesis that a vector of probabilities is uniformly
    # distributed.  Gary Robinson's original scheme was monotonic *with*
    # this test, but skipped the details.  Turns out that getting closer
    # to the theoretical roots gives a much sharper classification, with
    # a very small (in # of msgs), but also very broad (in range of scores),
    # "middle ground", where most of the mistakes live.  In particular,
    # this scheme seems immune to all forms of "cancellation disease":  if
    # there are many strong ham *and* spam clues, this reliably scores
    # close to 0.5.  Most other schemes are extremely certain then -- and
    # often wrong.
    def chi2_spamprob(self, wordstream, evidence=False):
        """Return best-guess probability that wordstream is spam.

        wordstream is an iterable object producing words.
        The return value is a float in [0.0, 1.0].

        If optional arg evidence is True, the return value is a pair
            probability, evidence
        where evidence is a list of (word, probability) pairs.
        """

        from math import frexp, log as ln

        # We compute two chi-squared statistics, one for ham and one for
        # spam.  The sum-of-the-logs business is more sensitive to probs
        # near 0 than to probs near 1, so the spam measure uses 1-p (so
        # that high-spamprob words have greatest effect), and the ham
        # measure uses p directly (so that lo-spamprob words have greatest
        # effect).
        #
        # For optimization, sum-of-logs == log-of-product, and f.p.
        # multiplication is a lot cheaper than calling ln().  It's easy
        # to underflow to 0.0, though, so we simulate unbounded dynamic
        # range via frexp.  The real product H = this H * 2**Hexp, and
        # likewise the real product S = this S * 2**Sexp.
        H = S = 1.0
        Hexp = Sexp = 0

        clues = self._getclues(wordstream)
        for prob, word, record in clues:
            S *= 1.0 - prob
            H *= prob
            if S < 1e-200:  # prevent underflow
                S, e = frexp(S)
                Sexp += e
            if H < 1e-200:  # prevent underflow
                H, e = frexp(H)
                Hexp += e

        # Compute the natural log of the product = sum of the logs:
        # ln(x * 2**i) = ln(x) + i * ln(2).
        S = ln(S) + Sexp * LN2
        H = ln(H) + Hexp * LN2

        n = len(clues)
        if n:
            S = 1.0 - chi2Q(-2.0 * S, 2*n)
            H = 1.0 - chi2Q(-2.0 * H, 2*n)

            # How to combine these into a single spam score?  We originally
            # used (S-H)/(S+H) scaled into [0., 1.], which equals S/(S+H).  A
            # systematic problem is that we could end up being near-certain
            # a thing was (for example) spam, even if S was small, provided
            # that H was much smaller.
            # Rob Hooft stared at these problems and invented the measure
            # we use now, the simpler S-H, scaled into [0., 1.].
            prob = (S-H + 1.0) / 2.0
        else:
            prob = 0.5

        if evidence:
            clues = [(w, p) for p, w, _r in clues]
            #clues.sort(lambda a, b: cmp(a[1], b[1]))
            clues.sort(key=lambda c: c[1])
            clues.insert(0, ('*S*', S))
            clues.insert(0, ('*H*', H))
            return prob, clues
        else:
            return prob

    def slurping_spamprob(self, wordstream, evidence=False):
        """Do the standard chi-squared spamprob, but if the evidence
        leaves the score in the unsure range, and we have fewer tokens
        than max_discriminators, also generate tokens from the text
        obtained by following http URLs in the message."""
        h_cut = options["Categorization", "ham_cutoff"]
        s_cut = options["Categorization", "spam_cutoff"]

        # Get the raw score.
        prob, clues = self.chi2_spamprob(wordstream, True)

        # If necessary, enhance it with the tokens from whatever is
        # at the URL's destination.
        if len(clues) < options["Classifier", "max_discriminators"] and \
           prob > h_cut and prob < s_cut and slurp_wordstream:
            slurp_tokens = list(self._generate_slurp())
            slurp_tokens.extend([w for (w, _p) in clues])
            sprob, sclues = self.chi2_spamprob(slurp_tokens, True)
            if sprob < h_cut or sprob > s_cut:
                prob = sprob
                clues = sclues
        if evidence:
            return prob, clues
        return prob

    if options["Classifier", "use_chi_squared_combining"]:
        if options["URLRetriever", "x-slurp_urls"]:
            spamprob = slurping_spamprob
        else:
            spamprob = chi2_spamprob

    def learn(self, wordstream, is_spam):
        """Teach the classifier by example.

        wordstream is a word stream representing a message.  If is_spam is
        True, you're telling the classifier this message is definitely spam,
        else that it's definitely not spam.
        """
        if options["Classifier", "use_bigrams"]:
            wordstream = self._enhance_wordstream(wordstream)
        if options["URLRetriever", "x-slurp_urls"]:
            wordstream = self._add_slurped(wordstream)
        self._add_msg(wordstream, is_spam)

    def unlearn(self, wordstream, is_spam):
        """In case of pilot error, call unlearn ASAP after screwing up.

        Pass the same arguments you passed to learn().
        """
        if options["Classifier", "use_bigrams"]:
            wordstream = self._enhance_wordstream(wordstream)
        if options["URLRetriever", "x-slurp_urls"]:
            wordstream = self._add_slurped(wordstream)
        self._remove_msg(wordstream, is_spam)

    def probability(self, record):
        """Compute, store, and return prob(msg is spam | msg contains word).

        This is the Graham calculation, but stripped of biases, and
        stripped of clamping into 0.01 thru 0.99.  The Bayesian
        adjustment following keeps them in a sane range, and one
        that naturally grows the more evidence there is to back up
        a probability.
        """

        spamcount = record.spamcount
        hamcount = record.hamcount

        # Try the cache first
        try:
            return self.probcache[spamcount][hamcount]
        except KeyError:
            pass

        nham = float(self.nham or 1)
        nspam = float(self.nspam or 1)

        #assert hamcount <= nham, "Token seen in more ham than ham trained."
        hamratio = hamcount / nham
        if hamratio > 1.0:
            hamratio = 1.0

        #assert spamcount <= nspam, "Token seen in more spam than spam trained."
        spamratio = spamcount / nspam
        if spamratio > 1.0:
            spamratio = 1.0

        prob = spamratio / (hamratio + spamratio)

        S = options["Classifier", "unknown_word_strength"]
        StimesX = S * options["Classifier", "unknown_word_prob"]


        # Now do Robinson's Bayesian adjustment.
        #
        #         s*x + n*p(w)
        # f(w) = --------------
        #           s + n
        #
        # I find this easier to reason about like so (equivalent when
        # s != 0):
        #
        #        x - p
        #  p +  -------
        #       1 + n/s
        #
        # IOW, it moves p a fraction of the distance from p to x, and
        # less so the larger n is, or the smaller s is.

        n = hamcount + spamcount
        prob = (StimesX + n * prob) / (S + n)

        # Update the cache
        try:
            self.probcache[spamcount][hamcount] = prob
        except KeyError:
            self.probcache[spamcount] = {hamcount: prob}

        return prob

    # NOTE:  Graham's scheme had a strange asymmetry:  when a word appeared
    # n>1 times in a single message, training added n to the word's hamcount
    # or spamcount, but predicting scored words only once.  Tests showed
    # that adding only 1 in training, or scoring more than once when
    # predicting, hurt under the Graham scheme.
    # This isn't so under Robinson's scheme, though:  results improve
    # if training also counts a word only once.  The mean ham score decreases
    # significantly and consistently, ham score variance decreases likewise,
    # mean spam score decreases (but less than mean ham score, so the spread
    # increases), and spam score variance increases.
    # I (Tim) speculate that adding n times under the Graham scheme helped
    # because it acted against the various ham biases, giving frequently
    # repeated spam words (like "Viagra") a quick ramp-up in spamprob; else,
    # adding only once in training, a word like that was simply ignored until
    # it appeared in 5 distinct training spams.  Without the ham-favoring
    # biases, though, and never ignoring words, counting n times introduces
    # a subtle and unhelpful bias.
    # There does appear to be some useful info in how many times a word
    # appears in a msg, but distorting spamprob doesn't appear a correct way
    # to exploit it.
    def _add_msg(self, wordstream, is_spam):
        self.probcache = {}    # nuke the prob cache
        if is_spam:
            self.nspam += 1
        else:
            self.nham += 1

        for word in set(wordstream):
            record = self._wordinfoget(word)
            if record is None:
                record = self.WordInfoClass()

            if is_spam:
                record.spamcount += 1
            else:
                record.hamcount += 1

            self._wordinfoset(word, record)

        self._post_training()

    def _remove_msg(self, wordstream, is_spam):
        self.probcache = {}    # nuke the prob cache
        if is_spam:
            if self.nspam > 0:
                self.nspam -= 1
            else:
                pass
                print("Warning: spam count would go negative!", file=sys.stderr)
                #raise ValueError("spam count would go negative!")
        else:
            if self.nham > 0:
                self.nham -= 1
            else:
                print("Warning: non-spam count would go negative!", file=sys.stderr)
                #raise ValueError("non-spam count would go negative!")

        for word in set(wordstream):
            record = self._wordinfoget(word)
            if record is not None:
                if is_spam:
                    if record.spamcount > 0:
                        record.spamcount -= 1
                else:
                    if record.hamcount > 0:
                        record.hamcount -= 1
                if record.hamcount == 0 == record.spamcount:
                    self._wordinfodel(word)
                else:
                    self._wordinfoset(word, record)

        self._post_training()

    def _post_training(self):
        """This is called after training on a wordstream.  Subclasses might
        want to ensure that their databases are in a consistent state at
        this point.  Introduced to fix bug #797890."""
        pass

    # Return list of (prob, word, record) triples, sorted by increasing
    # prob.  "word" is a token from wordstream; "prob" is its spamprob (a
    # float in 0.0 through 1.0); and "record" is word's associated
    # WordInfo record if word is in the training database, or None if it's
    # not.  No more than max_discriminators items are returned, and have
    # the strongest (farthest from 0.5) spamprobs of all tokens in wordstream.
    # Tokens with spamprobs less than minimum_prob_strength away from 0.5
    # aren't returned.
    def _getclues(self, wordstream):
        mindist = options["Classifier", "minimum_prob_strength"]

        if options["Classifier", "use_bigrams"]:
            # This scheme mixes single tokens with pairs of adjacent tokens.
            # wordstream is "tiled" into non-overlapping unigrams and
            # bigrams.  Non-overlap is important to prevent a single original
            # token from contributing to more than one spamprob returned
            # (systematic correlation probably isn't a good thing).

            # First fill list raw with
            #     (distance, prob, word, record), indices
            # pairs, one for each unigram and bigram in wordstream.
            # indices is a tuple containing the indices (0-based relative to
            # the start of wordstream) of the tokens that went into word.
            # indices is a 1-tuple for an original token, and a 2-tuple for
            # a synthesized bigram token.  The indices are needed to detect
            # overlap later.
            raw = []
            push = raw.append
            pair = None
            # Keep track of which tokens we've already seen.
            # Don't use a set here!  This is an innermost loop, so speed is
            # important here (direct dict fiddling is much quicker than
            # invoking Python-level set methods; in Python 2.4 that will
            # change).
            seen = {pair: 1} # so the bigram token is skipped on 1st loop trip
            for i, token in enumerate(wordstream):
                if i:   # not the 1st loop trip, so there is a preceding token
                    # This string interpolation must match the one in
                    # _enhance_wordstream().
                    pair = "bi:%s %s" % (last_token, token)
                last_token = token
                for clue, indices in (token, (i,)), (pair, (i-1, i)):
                    if clue not in seen:    # as always, skip duplicates
                        seen[clue] = 1
                        tup = self._worddistanceget(clue)
                        if tup[0] >= mindist:
                            push((tup, indices))

            # Sort raw, strongest to weakest spamprob.
            raw.sort()
            raw.reverse()
            # Fill clues with the strongest non-overlapping clues.
            clues = []
            push = clues.append
            # Keep track of which indices have already contributed to a
            # clue in clues.
            seen = {}
            for tup, indices in raw:
                overlap = [i for i in indices if i in seen]
                if not overlap: # no overlap with anything already in clues
                    for i in indices:
                        seen[i] = 1
                    push(tup)
            # Leave sorted from smallest to largest spamprob.
            clues.reverse()

        else:
            # The all-unigram scheme just scores the tokens as-is.  A set()
            # is used to weed out duplicates at high speed.
            clues = []
            push = clues.append
            for word in set(wordstream):
                tup = self._worddistanceget(word)
                if tup[0] >= mindist:
                    push(tup)
            clues.sort()

        if len(clues) > options["Classifier", "max_discriminators"]:
            del clues[0 : -options["Classifier", "max_discriminators"]]
        # Return (prob, word, record).
        return [t[1:] for t in clues]

    def _worddistanceget(self, word):
        record = self._wordinfoget(word)
        if record is None:
            prob = options["Classifier", "unknown_word_prob"]
        else:
            prob = self.probability(record)
        distance = abs(prob - 0.5)
        return distance, prob, word, record

    def _wordinfoget(self, word):
        return self.wordinfo.get(word)

    def _wordinfoset(self, word, record):
        self.wordinfo[word] = record

    def _wordinfodel(self, word):
        del self.wordinfo[word]

    def _enhance_wordstream(self, wordstream):
        """Add bigrams to the wordstream.

        For example, a b c -> a b "a b" c "b c"

        Note that these are *token* bigrams, and not *word* bigrams - i.e.
        'synthetic' tokens get bigram'ed, too.

        The bigram token is simply "bi:unigram1 unigram2" - a space should
        be sufficient as a separator, since spaces aren't in any other
        tokens, apart from 'synthetic' ones.  The "bi:" prefix is added
        to avoid conflict with tokens we generate (like "subject: word",
        which could be "word" in a subject, or a bigram of "subject:" and
        "word").

        If the "Classifier":"use_bigrams" option is removed, this function
        can be removed, too.
        """

        last = None
        for token in wordstream:
            yield token
            if last:
                # This string interpolation must match the one in
                # _getclues().
                yield "bi:%s %s" % (last, token)
            last = token

    def _generate_slurp(self):
        # We don't want to do this recursively and check URLs
        # on webpages, so we have this little cheat.
        if not hasattr(self, "setup_done"):
            self.setup()
            self.setup_done = True
        if not hasattr(self, "do_slurp") or self.do_slurp:
            if slurp_wordstream:
                self.do_slurp = False

                tokens = self.slurp(*slurp_wordstream)
                self.do_slurp = True
                self._save_caches()
                return tokens
        return []

    def setup(self):
        # Can't import this at the top because it's circular.
        # XXX Someone smarter than me, please figure out the right
        # XXX way to do this.
        from spambayes.FileCorpus import ExpiryFileCorpus, FileMessageFactory

        username = options["globals", "proxy_username"]
        password = options["globals", "proxy_password"]
        server = options["globals", "proxy_server"]
        if server.find(":") != -1:
            server, port = server.split(':', 1)
        else:
            port = 8080
        if server:
            # Build a new opener that uses a proxy requiring authorization
            proxy_support = urllib.request.ProxyHandler({"http" : \
                                                  "http://%s:%s@%s:%d" % \
                                                  (username, password,
                                                   server, port)})
            opener = urllib.request.build_opener(proxy_support,
                                          urllib.request.HTTPHandler)
        else:
            # Build a new opener without any proxy information.
            opener = urllib.request.build_opener(urllib.request.HTTPHandler)

        # Install it
        urllib.request.install_opener(opener)

        # Setup the cache for retrieved urls
        age = options["URLRetriever", "x-cache_expiry_days"]*24*60*60
        dir = options["URLRetriever", "x-cache_directory"]
        if not os.path.exists(dir):
            # Create the directory.
            if options["globals", "verbose"]:
                print("Creating URL cache directory", file=sys.stderr)
            os.makedirs(dir)

        self.urlCorpus = ExpiryFileCorpus(age, FileMessageFactory(),
                                          dir, cacheSize=20)
        # Kill any old information in the cache
        self.urlCorpus.removeExpiredMessages()

        # Setup caches for unretrievable urls
        self.bad_url_cache_name = os.path.join(dir, "bad_urls.pck")
        self.http_error_cache_name = os.path.join(dir, "http_error_urls.pck")
        if os.path.exists(self.bad_url_cache_name):
            try:
                self.bad_urls = pickle_read(self.bad_url_cache_name)
            except (IOError, ValueError):
                # Something went wrong loading it (bad pickle,
                # probably).  Start afresh.
                if options["globals", "verbose"]:
                    print("Bad URL pickle, using new.", file=sys.stderr)
                self.bad_urls = {"url:non_resolving": (),
                                 "url:non_html": (),
                                 "url:unknown_error": ()}
        else:
            if options["globals", "verbose"]:
                print("URL caches don't exist: creating")
            self.bad_urls = {"url:non_resolving": (),
                        "url:non_html": (),
                        "url:unknown_error": ()}
        if os.path.exists(self.http_error_cache_name):
            try:
                self.http_error_urls = pickle_read(self.http_error_cache_name)
            except (IOError, ValueError):
                # Something went wrong loading it (bad pickle,
                # probably).  Start afresh.
                if options["globals", "verbose"]:
                    print("Bad HHTP error pickle, using new.", file=sys.stderr)
                self.http_error_urls = {}
        else:
            self.http_error_urls = {}

    def _save_caches(self):
        # XXX Note that these caches are never refreshed, which might not
        # XXX be a good thing long-term (if a previously invalid URL
        # XXX becomes valid, for example).
        for name, data in [(self.bad_url_cache_name, self.bad_urls),
                           (self.http_error_cache_name, self.http_error_urls),]:
            pickle_write(name, data)

    def slurp(self, proto, url):
        # We generate these tokens:
        #  url:non_resolving
        #  url:non_html
        #  url:http_XXX (for each type of http error encounted,
        #                for example 404, 403, ...)
        # And tokenise the received page (but we do not slurp this).
        # Actually, the special url: tokens barely showed up in my testing,
        # although I would have thought that they would more - this might
        # be due to an error, although they do turn up on occasion.  In
        # any case, we have to do the test, so generating an extra token
        # doesn't cost us anything apart from another entry in the db, and
        # it's only two entries, plus one for each type of http error
        # encountered, so it's pretty neglible.
        # If there is no content in the URL, then just return immediately.
        # "http://)" will trigger this.
        if not url:
            return ["url:non_resolving"]

        from spambayes.tokenizer import Tokenizer

        if options["URLRetriever", "x-only_slurp_base"]:
            url = self._base_url(url)

        # Check the unretrievable caches
        for err in list(self.bad_urls.keys()):
            if url in self.bad_urls[err]:
                return [err]
        if url in self.http_error_urls:
            return self.http_error_urls[url]

        # We check if the url will resolve first
        mo = DOMAIN_AND_PORT_RE.match(url)
        domain = mo.group(1)
        if mo.group(3) is None:
            port = 80
        else:
            port = mo.group(3)
        try:
            _unused = socket.getaddrinfo(domain, port)
        except socket.error:
            self.bad_urls["url:non_resolving"] += (url,)
            return ["url:non_resolving"]

        # If the message is in our cache, then we can just skip over
        # retrieving it from the network, and get it from there, instead.
        url_key = URL_KEY_RE.sub('_', url)
        cached_message = self.urlCorpus.get(url_key)

        if cached_message is None:
            # We're going to ignore everything that isn't text/html,
            # so we might as well not bother retrieving anything with
            # these extensions.
            parts = url.split('.')
            if parts[-1] in ('jpg', 'gif', 'png', 'css', 'js'):
                self.bad_urls["url:non_html"] += (url,)
                return ["url:non_html"]

            # Waiting for the default timeout period slows everything
            # down far too much, so try and reduce it for just this
            # call (this will only work with Python 2.3 and above).
            try:
                timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(5)
            except AttributeError:
                # Probably Python 2.2.
                pass
            try:
                if options["globals", "verbose"]:
                    print("Slurping", url, file=sys.stderr)
                f = urllib.request.urlopen("%s://%s" % (proto, url))
            except (urllib.error.URLError, socket.error) as details:
                mo = HTTP_ERROR_RE.match(str(details))
                if mo:
                    self.http_error_urls[url] = "url:http_" + mo.group(1)
                    return ["url:http_" + mo.group(1)]
                self.bad_urls["url:unknown_error"] += (url,)
                return ["url:unknown_error"]
            # Restore the timeout
            try:
                socket.setdefaulttimeout(timeout)
            except AttributeError:
                # Probably Python 2.2.
                pass

            try:
                # Anything that isn't text/html is ignored
                content_type = f.info().get('content-type')
                if content_type is None or \
                   not content_type.startswith("text/html"):
                    self.bad_urls["url:non_html"] += (url,)
                    return ["url:non_html"]

                page = f.read()
                headers = str(f.info())
                f.close()
            except socket.error:
                # This is probably a temporary error, like a timeout.
                # For now, just bail out.
                return []

            fake_message_string = headers + "\r\n" + page

            # Retrieving the same messages over and over again will tire
            # us out, so we store them in our own wee cache.
            message = self.urlCorpus.makeMessage(url_key,
                                                 fake_message_string)
            self.urlCorpus.addMessage(message)
        else:
            fake_message_string = cached_message.as_string()

        msg = message_from_string(fake_message_string)

        # We don't want to do full header tokenising, as this is
        # optimised for messages, not webpages, so we just do the
        # basic stuff.
        bht = options["Tokenizer", "basic_header_tokenize"]
        bhto = options["Tokenizer", "basic_header_tokenize_only"]
        options["Tokenizer", "basic_header_tokenize"] = True
        options["Tokenizer", "basic_header_tokenize_only"] = True

        tokens = Tokenizer().tokenize(msg)
        pf = options["URLRetriever", "x-web_prefix"]
        tokens = ["%s%s" % (pf, tok) for tok in tokens]

        # Undo the changes
        options["Tokenizer", "basic_header_tokenize"] = bht
        options["Tokenizer", "basic_header_tokenize_only"] = bhto
        return tokens

    def _base_url(self, url):
        # To try and speed things up, and to avoid following
        # unique URLS, we convert the URL to as basic a form
        # as we can - so http://www.massey.ac.nz/~tameyer/index.html?you=me
        # would become http://massey.ac.nz and http://id.example.com
        # would become http://example.com
        url += '/'
        domain = url.split('/', 1)[0]
        parts = domain.split('.')
        if len(parts) > 2:
            base_domain = parts[-2] + '.' + parts[-1]
            if len(parts[-1]) < 3:
                base_domain = parts[-3] + '.' + base_domain
        else:
            base_domain = domain
        return base_domain

    def _add_slurped(self, wordstream):
        """Add tokens generated by 'slurping' (i.e. tokenizing
        the text at the web pages pointed to by URLs in messages)
        to the wordstream."""
        for token in wordstream:
            yield token
        slurped_tokens = self._generate_slurp()
        for token in slurped_tokens:
            yield token

    def _wordinfokeys(self):
        return list(self.wordinfo.keys())


Bayes = Classifier
