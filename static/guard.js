for(const name of ['nav','initHome','initAuth','initCitizen','initComplaints','initProject','initComplaintDetail','initRep','initElection','initAdmin']){
  const original=window[name];
  window[name]=function(...args){
    const key='gs_'+name;
    if(document.body.dataset[key])return;
    document.body.dataset[key]='1';
    return original(...args);
  };
}
