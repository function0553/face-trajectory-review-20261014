#pragma once
#include "face_trajectory/state.hpp"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace face_trajectory {
// Immutable AVL sequences. Versions share unchanged subtrees, including nested
// arrays. Evidence uses observation IDs as keys, so arbitrary IDs insert locally
// rather than shifting every subsequent leaf. All other arrays use ordinal keys.
// The cache lives for one operation; no reference can survive a transaction rollback.
class SharedJson {
    using J = nlohmann::json;
    State& state_;
    std::map<std::string,J> cache_;
    std::map<std::string,J> values_;
    J Node(const std::string& ref) {
        if(ref.empty()) return {{"h",0}};
        auto found=cache_.find(ref);
        if(found!=cache_.end()) return found->second;
        auto text=state_.Get("object",ref);
        if(!text) throw std::runtime_error("missing shared trajectory node");
        auto node=J::parse(*text);
        if(node.contains("value_ref")) {
            const auto value_ref=node.at("value_ref").get<std::string>();
            const auto found=values_.find(value_ref);
            if(found!=values_.end()) node["v"]=found->second;
            else {
                const auto value=state_.Get("object",value_ref);
                if(!value) throw std::runtime_error("missing shared trajectory value");
                node["v"]=J::parse(*value); values_.emplace(value_ref,node.at("v"));
            }
            node.erase("value_ref");
        }
        cache_.emplace(ref,node); return node;
    }
    int Height(const std::string& ref) { return Node(ref).at("h"); }
    std::string Make(const std::string& key,const J& value,const std::string& left,const std::string& right) {
        J node={{"k",key},{"v",value},{"l",left},{"r",right},{"h",1+std::max(Height(left),Height(right))}};
        auto stored=node;
        // A path copy references an evidence body once, instead of copying that
        // body's fields at every ancestor/version. Small scalar values stay inline.
        if(value.is_object() || value.is_array()) {
            const auto ref=state_.Intern(value.dump()); values_.emplace(ref,value);
            stored.erase("v"); stored["value_ref"]=ref;
        }
        const auto ref=state_.Intern(stored.dump()); cache_.emplace(ref,std::move(node)); return ref;
    }
    std::string Balance(const std::string& key,const J& value,std::string left,std::string right) {
        if(Height(left)>Height(right)+1) {
            auto a=Node(left);
            if(Height(a.at("r"))>Height(a.at("l"))) {
                const auto b=Node(a.at("r"));
                left=Make(b.at("k"),b.at("v"),Make(a.at("k"),a.at("v"),a.at("l"),b.at("l")),b.at("r"));
                a=Node(left);
            }
            return Make(a.at("k"),a.at("v"),a.at("l"),Make(key,value,a.at("r"),right));
        }
        if(Height(right)>Height(left)+1) {
            auto a=Node(right);
            if(Height(a.at("l"))>Height(a.at("r"))) {
                const auto b=Node(a.at("l"));
                right=Make(b.at("k"),b.at("v"),b.at("l"),Make(a.at("k"),a.at("v"),b.at("r"),a.at("r")));
                a=Node(right);
            }
            return Make(a.at("k"),a.at("v"),Make(key,value,left,a.at("l")),a.at("r"));
        }
        return Make(key,value,left,right);
    }
    std::string Put(const std::string& ref,const std::string& key,const J& value) {
        if(ref.empty()) return Make(key,value,"","");
        const auto node=Node(ref); const auto oldkey=node.at("k").get<std::string>();
        if(key==oldkey) return Make(key,value,node.at("l"),node.at("r"));
        if(key<oldkey) return Balance(oldkey,node.at("v"),Put(node.at("l"),key,value),node.at("r"));
        return Balance(oldkey,node.at("v"),node.at("l"),Put(node.at("r"),key,value));
    }
    std::optional<J> Find(std::string ref,const std::string& key) {
        while(!ref.empty()) { const auto node=Node(ref); const auto k=node.at("k").get<std::string>();
            if(key==k) return node.at("v"); ref=node.at(key<k?"l":"r").get<std::string>(); }
        return {};
    }
    std::string Erase(const std::string& ref,const std::string& key) {
        if(ref.empty()) return ref;
        const auto node=Node(ref); const auto k=node.at("k").get<std::string>();
        if(key<k) return Balance(k,node.at("v"),Erase(node.at("l"),key),node.at("r"));
        if(key>k) return Balance(k,node.at("v"),node.at("l"),Erase(node.at("r"),key));
        if(node.at("l")=="") return node.at("r");
        if(node.at("r")=="") return node.at("l");
        auto successor=Node(node.at("r"));
        while(successor.at("l")!="") successor=Node(successor.at("l"));
        return Balance(successor.at("k"),successor.at("v"),node.at("l"),Erase(node.at("r"),successor.at("k")));
    }
    void Keys(const std::string& ref,std::set<std::string>& keys) {
        if(ref.empty()) return;
        const auto node=Node(ref); keys.insert(node.at("k")); Keys(node.at("l"),keys); Keys(node.at("r"),keys);
    }
    std::string Build(const std::vector<std::pair<std::string,J>>& items,std::size_t lo,std::size_t hi) {
        if(lo==hi) return "";
        const auto mid=lo+(hi-lo)/2;
        return Make(items[mid].first,items[mid].second,Build(items,lo,mid),Build(items,mid+1,hi));
    }
    void Values(const std::string& ref,J& values) {
        if(ref.empty()) return;
        const auto node=Node(ref); Values(node.at("l"),values); values.push_back(Decode(node.at("v"))); Values(node.at("r"),values);
    }
    static std::string Ordinal(std::size_t index) {
        auto key=std::to_string(index); return std::string(20-key.size(),'0')+key;
    }
public:
    explicit SharedJson(State& state):state_(state) {}
    J Encode(const J& value,bool evidence=false,const std::optional<J>& previous={}) {
        if(value.is_array()) {
            if(previous && previous->is_object() && previous->contains("$avl") && previous->at("keyed")==evidence) {
                auto root=previous->at("$avl").get<std::string>();
                std::set<std::string> removed; Keys(root,removed);
                for(std::size_t i=0;i<value.size();++i) {
                    const auto key=evidence?value[i].at("observation_id").get<std::string>():Ordinal(i);
                    const auto old=Find(root,key); const auto encoded=Encode(value[i],false,old);
                    if(!old || *old!=encoded) root=Put(root,key,encoded);
                    removed.erase(key);
                }
                for(const auto& key:removed) root=Erase(root,key);
                return {{"$avl",root},{"count",value.size()},{"keyed",evidence}};
            }
            std::vector<std::pair<std::string,J>> items; items.reserve(value.size());
            for(std::size_t i=0;i<value.size();++i)
                items.emplace_back(evidence?value[i].at("observation_id").get<std::string>():Ordinal(i),Encode(value[i]));
            if(evidence) std::sort(items.begin(),items.end(),[](const auto& a,const auto& b){return a.first<b.first;});
            return {{"$avl",Build(items,0,items.size())},{"count",items.size()},{"keyed",evidence}};
        }
        if(value.is_object()) { J result=J::object();
            for(auto it=value.begin();it!=value.end();++it) result[it.key()]=Encode(it.value(),it.key()=="evidence",
                previous && previous->is_object() && previous->contains(it.key())?std::optional<J>(previous->at(it.key())):std::nullopt);
            return result;
        }
        return value;
    }
    J Decode(const J& value) {
        if(value.is_object() && value.contains("$avl")) {
            J result=J::array(); Values(value.at("$avl"),result);
            if(result.size()!=Count(value)) throw std::runtime_error("shared trajectory sequence count mismatch");
            return result;
        }
        if(value.is_object()) { J result=J::object(); for(auto it=value.begin();it!=value.end();++it) result[it.key()]=Decode(it.value()); return result; }
        return value;
    }
    static std::size_t Count(const J& sequence) { return sequence.at("count").get<std::size_t>(); }
    J At(const J& sequence,std::size_t index) {
        if(sequence.at("keyed").get<bool>() || index>=Count(sequence)) throw std::out_of_range("shared sequence index");
        const auto value=Find(sequence.at("$avl"),Ordinal(index));
        if(!value) throw std::runtime_error("missing shared sequence entry"); return *value;
    }
    void Set(J& sequence,std::size_t index,const J& encoded) {
        if(sequence.at("keyed").get<bool>() || index>Count(sequence)) throw std::out_of_range("shared sequence index");
        sequence["$avl"]=Put(sequence.at("$avl"),Ordinal(index),encoded);
        if(index==Count(sequence)) sequence["count"]=index+1;
    }
    void Append(J& sequence,const J& value) { Set(sequence,Count(sequence),Encode(value)); }
    void InsertEvidence(J& sequence,const J& evidence) {
        const auto key=evidence.at("observation_id").get<std::string>();
        if(!sequence.at("keyed").get<bool>() || Find(sequence.at("$avl"),key)) throw std::logic_error("append proof reused an existing observation");
        sequence["$avl"]=Put(sequence.at("$avl"),key,Encode(evidence)); sequence["count"]=Count(sequence)+1;
    }
};
}
