// Run in the page by single-file (--browser-script) for the IEEE
// Xplore capture. The References fold is closed until clicked, and a
// closed fold is captured as its toggle button alone, leaving the
// book without a bibliography. The folds are items of one accordion
// (Authors, Figures, References, Citations, Footnotes), one open at a
// time and each a route of the page; a closed item's panel is removed
// from the page, so one capture holds one fold: this script opens
// References and pages through its "View More" button until the
// list is complete.
//
// The clicks happen just before the capture, once the reading view
// has rendered and attached its handlers (a click at document start
// reloads the route). SingleFile's user-script API: the init event
// asks SingleFile to send the before-capture request; preventDefault
// on it makes SingleFile wait for the response event.
dispatchEvent(new CustomEvent("single-file-user-script-init"));

var POLL = 500;

// call done() once test() holds, or after limit ms
function waitFor(test, limit, done) {
  var started = Date.now();
  var timer = setInterval(function () {
    if (test() || Date.now() - started > limit) {
      clearInterval(timer);
      done();
    }
  }, POLL);
}

function toggle() {
  return document.querySelector("button#references");
}

function isOpen() {
  var button = toggle();
  return !!button && button.getAttribute("aria-expanded") === "true";
}

function entries() {
  return document.querySelectorAll("div#references div.number").length;
}

// click "View More" until it is gone, each click waiting for the
// entries it adds
function loadAll(done) {
  var panel = document.querySelector("div#references");
  var more = panel && Array.prototype.find.call(
    panel.querySelectorAll("button"),
    function (b) { return /view more/i.test(b.textContent); });
  if (!more) {
    done();
    return;
  }
  var before = entries();
  more.click();
  waitFor(function () { return entries() > before; }, 15000,
          function () { loadAll(done); });
}

addEventListener("single-file-on-before-capture-request", function (event) {
  event.preventDefault();
  var respond = function () {
    dispatchEvent(new CustomEvent("single-file-on-before-capture-response"));
  };
  waitFor(function () { return !!toggle(); }, 30000, function () {
    if (!toggle()) {
      respond();
      return;
    }
    if (!isOpen()) {
      toggle().click();
    }
    waitFor(function () { return isOpen() && entries() > 0; }, 15000,
            function () { loadAll(function () { setTimeout(respond, 2000); }); });
  });
});
